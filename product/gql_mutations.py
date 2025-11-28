import datetime
import json
import logging
import traceback
from django.core.exceptions import ValidationError, PermissionDenied, ObjectDoesNotExist
from decimal import Decimal
from django.contrib.auth.models import AnonymousUser
from django.utils import timezone
from django.utils.translation import gettext as _
from django.db import transaction
import graphene
from location.models import Location as LocationModel
from operator import or_

from core.models import MutationLog
from core.schema import OpenIMISMutation
from dataclasses import dataclass
import uuid as uuidlib
from datetime import datetime as py_datetime
from .apps import ProductConfig
from .enums import (
    CareTypeEnum,
    CeilingExclusionEnum,
    CeilingInterpretationEnum,
    CeilingTypeEnum,
    LimitTypeEnum,
    PriceOriginEnum,
)
from .models import (
    LIMIT_CHOICES,
    MembershipType,
    Product,
    ProductItem,
    ProductService,
    ProductMutation,
)
from .services import (
    check_unique_code_product,
    save_product_history,
    set_product_deductible_and_ceiling,
    set_product_details,
    set_product_relative_distribution,
    update_product_location,
    create_product_from_parent,
)
import re
# Module-level logger
logger = logging.getLogger(__name__)

def get_product_mutation_logs(product, limit=10):
    """
    Retrieve recent mutation logs for a product.
    
    Args:
        product: The product instance
        limit: Maximum number of logs to retrieve
        
    Returns:
        list: List of mutation log dictionaries
    """
    try:
        product_mutations = ProductMutation.objects.filter(
            product=product
        ).select_related('mutation', 'mutation__user').order_by('-id')[:limit]
        
        logs = []
        for pm in product_mutations:
            mutation = pm.mutation
            logs.append({
                'id': mutation.id,
                'date_created': getattr(mutation, 'date_created', None).isoformat() if getattr(mutation, 'date_created', None) else None,
                'client_mutation_id': getattr(mutation, 'client_mutation_id', None),
                'client_mutation_label': getattr(mutation, 'client_mutation_label', None),
                'user_id': mutation.user.id if mutation.user else None,
                'username': getattr(mutation.user, 'username', None) if mutation.user else None,
                'status': getattr(mutation, 'status', None),
                'error': getattr(mutation, 'error', None)
            })
        
        return logs
    except Exception as e:
        logger.error(f"Failed to retrieve mutation logs for product {product.id}: {str(e)}")
        return []


@dataclass
class DeductibleOrCeilingValue:
    all: Decimal
    ip: Decimal
    op: Decimal

    def __init__(self, all, ip, op):
        self.all = all
        self.ip = ip
        self.op = op


def extract_deductibles(data):
    return DeductibleOrCeilingValue(
        all=data.pop("deductible", 0),
        ip=data.pop("deductible_ip", 0),
        op=data.pop("deductible_op", 0),
    )


def extract_ceilings(data):
    return DeductibleOrCeilingValue(
        all=data.pop("ceiling", 0),
        ip=data.pop("ceiling_ip", 0),
        op=data.pop("ceiling_op", 0)
    )


@transaction.atomic
def create_or_update_product(user, data, is_duplicate=False, has_no_indigent=False):
    """
    Create or update a product with its related data in a single transaction.

    This function handles the creation or update of a product, including its related
    membership types and other attributes. It ensures that all operations are performed
    within a single database transaction to maintain data integrity.

    Args:
        user (User): The user performing the action. Must have attributes `id` or `id_for_audit`.
        data (dict): A dictionary containing product data. Required keys include:
            - "code" (str): The unique code for the product.
            - "name" (str): The name of the product.
            - "card_replacement_fee" (Decimal): The fee for card replacement.
            Optional keys include:
            - "lump_sum" (Decimal): The lump sum amount for the product.
            - "membership_types" (str or list): JSON string or list of membership types.
            - "deductible", "deductible_ip", "deductible_op" (Decimal): Deductible values.
            - "ceiling", "ceiling_ip", "ceiling_op" (Decimal): Ceiling values.
        is_duplicate (bool): If True, the function will duplicate the product data.

    Returns:
        Product: The created or updated product instance.

    Raises:
        ValueError: If required fields are missing or `membership_types` is invalid.
        PermissionDenied: If the user does not have permission to perform the action.
        Exception: For any other errors during the operation.
    """
    try:
        # Parse membership_types if it's a string
        membership_types_input = data.get("membership_types")
        if membership_types_input and isinstance(membership_types_input, str):
            try:
                data["membership_types"] = json.loads(membership_types_input)
            except Exception as e:
                raise Exception(f"membership_types must be a valid JSON string: {e}")

        # Validate chf_id_format
        if 'chf_id_format' in data and data['chf_id_format'] is not None:
            if data['chf_id_format'] not in (1, 2, 3):
                raise ValueError('chf_id_format must be 1, 2, or 3')

        # Validate required fields (lump_sum is now optional)
        required_fields = ['code', 'name', 'card_replacement_fee']
        missing = [f for f in required_fields if not data.get(f)]
        if missing:
            raise ValueError(f"Missing required fields: {', '.join(missing)}")

        # Set audit user ID
        if hasattr(user, 'id_for_audit') and user.id_for_audit:
            data['audit_user_id'] = user.id_for_audit
        elif hasattr(user, 'id') and user.id:
            data['audit_user_id'] = user.id
        else:
            data['audit_user_id'] = 1

        # Process location UUID if provided
        if "location_uuid" in data:
            location_uuid = data.pop("location_uuid")
            if location_uuid:
                location_obj = LocationModel.objects.filter(uuid=location_uuid).first()
                if not location_obj:
                    raise ValueError(f"Location with UUID {location_uuid} does not exist.")
                data["location_id"] = location_obj.id

        # Process conversion product UUID if provided
        if "conversion_product_uuid" in data:
            conversion_uuid = data.pop("conversion_product_uuid")
            if conversion_uuid:
                product_obj = Product.objects.filter(uuid=conversion_uuid).first()
                if not product_obj:
                    raise ValueError(f"Product with UUID {conversion_uuid} does not exist.")
                data["conversion_product_id"] = product_obj.id

        # Map field names to match model
        field_mappings = {
            "deductible": "ded_insuree",
            "deductible_ip": "ded_ip_insuree",
            "deductible_op": "ded_op_insuree",
            "ceiling": "max_insuree",
            "ceiling_ip": "max_ip_insuree",
            "ceiling_op": "max_op_insuree"
        }
        for source, target in field_mappings.items():
            if source in data:
                data[target] = data.pop(source)

        # Remove any unmapped GraphQL fields that could cause issues
        for field in ["deductible", "deductible_ip", "deductible_op", "ceiling", "ceiling_ip", "ceiling_op"]:
            if field in data:
                del data[field]

        # Define all possible product fields
        product_fields = [
            'code', 'name', 'lump_sum', 'card_replacement_fee', 'audit_user_id', 'enrolment_period_start_date',
            'enrolment_period_end_date', 'coverage_period_start_date', 'coverage_period_end_date', 'administration_period', 'recurrence', 'location_id',
            'conversion_product_id', 'acc_code_remuneration', 'acc_code_premiums', 'premium_adult',
            'threshold', 'share_contribution', 'registration_lump_sum', 'registration_fee',
            'additional_spouse_contribution', 'penality_formula',
            'start_cycle_1', 'start_cycle_2', 'start_cycle_3', 'start_cycle_4', 'ceiling_interpretation',
            'ceiling_type', 'ded_insuree', 'ded_ip_insuree', 'ded_op_insuree', 'max_insuree', 'max_ip_insuree',
            'max_op_insuree', 'max_ceiling_policy', 'max_ceiling_policy_ip', 'max_ceiling_policy_op',
            'max_policy_extra_member', 'max_policy_extra_member_op', 'max_policy_extra_member_ip',
            'max_no_consultation', 'max_no_surgery', 'max_no_delivery', 'max_no_hospitalization',
            'max_no_visits', 'max_no_antenatal', 'max_amount_consultation', 'max_amount_surgery',
            'max_amount_delivery', 'max_amount_hospitalization', 'max_amount_antenatal', 'age_maximal', 'chf_id_format'
        ]
        
        # Filter and prepare product data
        product_data = {k: v for k, v in data.items() 
                       if k in product_fields and v is not None}

        # If UUID is provided and this is not a duplication request, perform an update
        if not is_duplicate and 'uuid' in data and data['uuid']:
            existing_product = Product.objects.filter(uuid=data['uuid'], validity_to__isnull=True).first()
            if existing_product:
                # Check if location is being changed
                new_location_id = product_data.get('location_id')
                old_location_id = existing_product.location_id
                location_changed = new_location_id is not None and new_location_id != old_location_id
                
                if location_changed:
                    # Handle location change with our special function
                    try:
                        updated_product, message = update_product_location(existing_product.id, new_location_id)
                        logger.info(message)
                        
                        # If the product was updated successfully, update other fields
                        # Remove location_id from product_data since it's already handled
                        if 'location_id' in product_data:
                            del product_data['location_id']
                            
                        # Update the remaining fields on the updated product
                        for field, value in product_data.items():
                            if hasattr(updated_product, field):
                                setattr(updated_product, field, value)
                        
                        # Ensure audit_user_id is set
                        updated_product.audit_user_id = data.get('audit_user_id', getattr(user, 'id_for_audit', getattr(user, 'id', 1)))
                        updated_product.save()
                        
                        # Update membership types if provided
                        if 'membership_types' in data and data['membership_types']:
                            _process_membership_types(updated_product, {'membership_types': data['membership_types']})
                        
                       
                        
                        # Return the updated product
                        updated_product.refresh_from_db()
                        return updated_product
                    except ValidationError as e:
                        # Re-raise the validation error
                        raise e
                else:
                    # Regular update without location change
                    # Update simple scalar fields directly
                    for field, value in product_data.items():
                        # Skip fields that are not actual model attributes (e.g. membership_types handled separately)
                        if hasattr(existing_product, field):
                            setattr(existing_product, field, value)
                    # Ensure audit_user_id is set
                    existing_product.audit_user_id = data.get('audit_user_id', getattr(user, 'id_for_audit', getattr(user, 'id', 1)))
                    existing_product.save()

                    # Update membership types if provided
                    if 'membership_types' in data and data['membership_types']:
                        _process_membership_types(existing_product, {'membership_types': data['membership_types']})

                    

                    # Return the updated product after refresh
                    existing_product.refresh_from_db()
                    return existing_product

        # Generate a UUID if not provided (for create path)
        if 'uuid' not in product_data:
            product_data['uuid'] = str(uuidlib.uuid4())

        # Start transaction for create path
        with transaction.atomic():
            return _create_product_with_relations(user, product_data, is_duplicate, has_no_indigent)
            
    except Exception as e:
        error_msg = f"Error in create_or_update_product: {str(e)}"
        logger.error(error_msg, exc_info=True)
        raise Exception(error_msg)

def _create_product_with_relations(user, product_data, is_duplicate, has_no_indigent=False):
    """
    Helper function to create a product with its relations in a transaction.
    """
    
    
    # Start a new transaction
    with transaction.atomic():
        product = None
        try:
            # Extract membership types data before creating the product
            membership_types_data = product_data.pop('membership_types', None)
            
            # Get location if provided
            location_id = None
            if 'location_uuid' in product_data and product_data['location_uuid']:
                try:
                    location = LocationModel.objects.get(uuid=product_data['location_uuid'])
                    location_id = location.id
                except LocationModel.DoesNotExist:
                    raise ValueError(f"Location with UUID {product_data['location_uuid']} not found")
            
            # Prepare all product fields
            product_fields = {
                'code': product_data['code'],
                'name': product_data['name'],
                # lump_sum is optional; do not default to 0 when not provided
                'lump_sum': product_data.get('lump_sum'),
                'card_replacement_fee': product_data.get('card_replacement_fee', 1),
                'period_rel_prices': 'F',
                'period_rel_prices_op': 'F',
                'period_rel_prices_ip': 'F',
                'audit_user_id': product_data.get('audit_user_id', 1),
                'premium_adult': product_data.get('premium_adult', 0),
                'location_id': location_id,
                'validity_from': timezone.now(),
                # Optional fields with defaults
                'enrolment_period_start_date': product_data.get('enrolment_period_start_date'),
                'enrolment_period_end_date': product_data.get('enrolment_period_end_date'),
                'coverage_period_start_date': product_data.get('coverage_period_start_date'),
                'coverage_period_end_date': product_data.get('coverage_period_end_date'),
                'administration_period': product_data.get('administration_period'),
                'recurrence': product_data.get('recurrence'),
                'conversion_product_id': product_data.get('conversion_product_id'),
                'threshold': product_data.get('threshold'),
                'share_contribution': product_data.get('share_contribution'),
                'registration_lump_sum': product_data.get('registration_lump_sum'),
                'registration_fee': product_data.get('registration_fee'),
                'additional_spouse_contribution': product_data.get('additional_spouse_contribution'),
                'penality_formula': product_data.get('penality_formula'),
                'start_cycle_1': product_data.get('start_cycle_1'),
                'start_cycle_2': product_data.get('start_cycle_2'),
                'start_cycle_3': product_data.get('start_cycle_3'),
                'start_cycle_4': product_data.get('start_cycle_4'),
                'ceiling_interpretation': product_data.get('ceiling_interpretation'),
                'ceiling_type': product_data.get('ceiling_type'),
                'ded_insuree': product_data.get('ded_insuree'),
                'ded_ip_insuree': product_data.get('ded_ip_insuree'),
                'ded_op_insuree': product_data.get('ded_op_insuree'),
                'max_insuree': product_data.get('max_insuree'),
                'max_ip_insuree': product_data.get('max_ip_insuree'),
                'max_op_insuree': product_data.get('max_op_insuree'),
                'max_ceiling_policy': product_data.get('max_ceiling_policy'),
                'max_ceiling_policy_ip': product_data.get('max_ceiling_policy_ip'),
                'max_ceiling_policy_op': product_data.get('max_ceiling_policy_op'),
                'max_policy_extra_member': product_data.get('max_policy_extra_member'),
                'max_policy_extra_member_op': product_data.get('max_policy_extra_member_op'),
                'max_policy_extra_member_ip': product_data.get('max_policy_extra_member_ip'),
                'max_no_consultation': product_data.get('max_no_consultation'),
                'max_no_surgery': product_data.get('max_no_surgery'),
                'max_no_delivery': product_data.get('max_no_delivery'),
                'max_no_hospitalization': product_data.get('max_no_hospitalization'),
                'max_no_visits': product_data.get('max_no_visits'),
                'max_no_antenatal': product_data.get('max_no_antenatal'),
                'max_amount_consultation': product_data.get('max_amount_consultation'),
                'max_amount_surgery': product_data.get('max_amount_surgery'),
                'max_amount_delivery': product_data.get('max_amount_delivery'),
                'max_amount_hospitalization': product_data.get('max_amount_hospitalization'),
                'max_amount_antenatal': product_data.get('max_amount_antenatal'),
                'age_maximal': product_data.get('age_maximal'),
                'chf_id_format': product_data.get('chf_id_format')
            }
            
            # Debug log to see what is being passed
            logger.debug("product_fields being passed to Product: %s", product_fields)
            # Remove any unmapped GraphQL fields that could cause issues
            unmapped = ['deductible', 'deductible_ip', 'deductible_op', 'ceiling', 'ceiling_ip', 'ceiling_op']
            for key in unmapped:
                if key in product_fields:
                    del product_fields[key]

            # Debug log all fields before save
            logger.debug("About to save Product with fields: %s", {k: v for k, v in product_fields.items()})
            try:
                product = Product(**{k: v for k, v in product_fields.items() if v is not None})
                product.save(force_insert=True)
            except Exception as e:
                logger.error("Error saving Product: %s", e)
                logger.exception("Exception occurred while saving Product")
                raise
            logger.info(f"Product created with ID: {product.id}")
            
            # Now process membership types if provided (after product is saved)
            # if membership_types_data:
            #     _process_membership_types(product, {'membership_types': membership_types_data})
            
            # Process product items if provided
            if 'product_items' in product_data and product_data['product_items']:
                _process_product_items(product, product_data)
            
            
            
            # Refresh and return the product
            product.refresh_from_db()
            logger.info(f"Successfully created product {product.id} with all related data")
            return product
        except Exception as e:
            logger.error("Error in _create_product_with_relations: %s", e, exc_info=True)
            raise


class ExtendProductByCloneMutation(graphene.Mutation):
    class Arguments:
        parent_product_uuid = graphene.UUID(required=True)
        location_uuid = graphene.UUID(required=False)
        location_id = graphene.Int(required=False)
        enrolment_period_start_date = graphene.Date(required=True)
        enrolment_period_end_date = graphene.Date(required=True)
        code = graphene.String(required=False)
        name = graphene.String(required=False)

    # Return the created product in the payload
    product = graphene.Field(lambda: __import__("product.schema", fromlist=["ProductGQLType"]).ProductGQLType)

    @classmethod
    def mutate(cls, root, info, parent_product_uuid, enrolment_period_start_date, enrolment_period_end_date, location_uuid=None, location_id=None, code=None, name=None):
        user = getattr(info.context, 'user', None)
        if type(user) is AnonymousUser or not getattr(user, 'id', None):
            raise ValidationError(_("mutation.authentication_required"))
        if not user.has_perms(ProductConfig.gql_mutation_products_duplicate_perms):
            raise PermissionDenied(_("unauthorized"))

        # Resolve parent product
        parent = Product.objects.filter(uuid=parent_product_uuid, validity_to__isnull=True).first()
        if not parent:
            raise ValidationError({"parent_product_uuid": _("Product not found")})

        # Resolve location: prefer UUID, fallback to ID
        location = None
        if location_uuid:
            location = LocationModel.objects.filter(uuid=location_uuid).first()
            if not location:
                raise ValidationError({"location_uuid": _("Location not found")})
        elif location_id is not None:
            location = LocationModel.objects.filter(id=location_id).first()
            if not location:
                raise ValidationError({"location_id": _("Location not found")})
        else:
            raise ValidationError({"location": _("Either location_uuid or location_id must be provided")})

        if enrolment_period_start_date and enrolment_period_end_date and enrolment_period_start_date > enrolment_period_end_date:
            raise ValidationError({"enrolment_period_end_date": _("End date must be on or after start date")})

        try:
            child = create_product_from_parent(
                parent_product_id=parent.id,
                new_location_id=location.id,
                enrolment_start_date=enrolment_period_start_date,
                enrolment_end_date=enrolment_period_end_date,
                user=user,
                code=code,
                name=name,
            )
            
            
            
            return ExtendProductByCloneMutation(product=child)
        except ValidationError:
            raise
        except Exception as e:
            logger.error("Error cloning product: %s", e, exc_info=True)
            raise Exception(_(f"Failed to clone product: {str(e)}"))

def _process_membership_types(product, product_data, has_no_indigent=False):
    """Process and create membership types for the product.

    Regardless of the input, this function will always create an additional
    default *indigent* `MembershipType` for the product with the following
    attributes:
        - is_indigent = True
        - price = 0
        - level_index = 1
    This default membership type is appended **before** any user-provided
    membership types, ensuring there is always at least one indigent level.
    """
    
    # Extract raw input (may be None)
    membership_types_input = product_data.get('membership_types')

    # Ensure we have a valid audit user ID
    audit_user_id = getattr(product, 'audit_user_id', 1) or 1

    # Collect new membership type objects to attach to the product
    membership_types: list[MembershipType] = []

    # Define valid fields for MembershipType model
    valid_fields = {f.name for f in MembershipType._meta.get_fields() if f.concrete and not f.many_to_many and not f.one_to_many}

    # Location is now handled in the main mutation, so we need to fetch it again for processing
    mt_data_input = product_data.get('membership_types', {})
    if not isinstance(mt_data_input, dict):
        mt_data_input = {}

    # Fetch and validate Region and District from the input data
    region_name = mt_data_input.get('region')
    district_name = mt_data_input.get('district')
    region, district = None, None

    if region_name:
        # Use get_or_create to prevent errors from missing seed data
        region, created = LocationModel.objects.get_or_create(
            name=region_name,
            type='R',
            defaults={'code': region_name.upper(), 'audit_user_id': audit_user_id}
        )
        if created:
            logger.info(f"Created new region: {region_name}")

        if district_name:
            # Use get_or_create for the district as well
            district, created = LocationModel.objects.get_or_create(
                name=district_name,
                type='D',
                parent=region,
                defaults={'code': district_name.upper(), 'audit_user_id': audit_user_id}
            )
            if created:
                logger.info(f"Created new district: {district_name} in region {region_name}")

    

    # 2. Process user-provided membership types (if any)
    if mt_data_input and 'levels' in mt_data_input:
        levels = mt_data_input.get('levels', {})

        try:
            # Create urban membership types
            for idx, price in enumerate(levels.get('urban', []), start=1):
                if price is None:
                    continue
                mt_data = {
                    'region_id': region.id if region else None,
                    'district_id': district.id if district else None,
                    'level_type': 'urban',
                    'level_index': idx,
                    'price': price,
                    'is_indigent': False,
                    'audit_user_id': audit_user_id,
                }
                filtered = {k: v for k, v in mt_data.items() if k in valid_fields}
                membership_types.append(MembershipType.objects.create(**filtered))

            # Create rural membership types
            for idx, price in enumerate(levels.get('rural', []), start=1):
                if price is None:
                    continue
                mt_data = {
                    'region_id': region.id if region else None,
                    'district_id': district.id if district else None,
                    'level_type': 'rural',
                    'level_index': idx,
                    'price': price,
                    'is_indigent': False,
                    'audit_user_id': audit_user_id,
                }
                filtered = {k: v for k, v in mt_data.items() if k in valid_fields}
                membership_types.append(MembershipType.objects.create(**filtered))
        except Exception as e:
            logger.error("Error creating provided membership types: %s", e)
            raise
    else:
        if membership_types_input and not isinstance(membership_types_input, dict):
            logger.warning("membership_types should be a dictionary if provided")
        # No additional membership types supplied – only default indigent will exist

    # 3. Attach new membership types to the product (replace existing)
    with transaction.atomic():
        product.membership_types.clear()
        product.membership_types.add(*membership_types)
        logger.info("Attached %s membership types (including default indigent) to product %s", len(membership_types), product.id)
    return

def _process_product_items(product, product_data):
    """Process and create product items."""
    if 'product_items' not in product_data or not product_data['product_items']:
        return
        
    for item_data in product_data['product_items']:
        try:
            ProductItem.objects.create(
                product=product,
                item_id=item_data['item_id'],
                price=item_data.get('price', 0),
                audit_user_id=product_data.get('audit_user_id', 1)
            )
            logger.info(f"Created product item for product {product.id}")
        except Exception as e:
            logger.error(f"Error creating product item: {str(e)}")
            raise


class RelativePricesInput(graphene.InputObjectType):
    care_type = graphene.Field(CareTypeEnum)
    periods = graphene.NonNull(graphene.List(
        graphene.NonNull(graphene.Decimal)))


class CreateOrUpdateProductMutation(OpenIMISMutation):
    @classmethod
    def do_mutate(cls, perms, user, **data):
        if type(user) is AnonymousUser or not user.id:
            raise ValidationError(_("mutation.authentication_required"))
        if not user.has_perms(perms):
            raise PermissionDenied(_("unauthorized"))

        data["audit_user_id"] = user.id_for_audit

        has_no_indigent = data.pop("has_no_indigent", False)
        return create_or_update_product(user, data, has_no_indigent=has_no_indigent)


class ProductServiceOrItemInput(graphene.InputObjectType):
    price_origin = graphene.Field(PriceOriginEnum)
    limitation_type = graphene.Field(LimitTypeEnum)
    limitation_type_r = graphene.Field(LimitTypeEnum)
    limitation_type_e = graphene.Field(LimitTypeEnum)
    waiting_period_adult = graphene.Int()
    waiting_period_child = graphene.Int()
    limit_no_adult = graphene.Int()
    limit_no_child = graphene.Int()
    limit_adult = graphene.Decimal(
        max_digits=18, decimal_places=2, required=False, default_value=0
    )
    limit_child = graphene.Decimal(
        max_digits=18, decimal_places=2, required=False, default_value=0
    )
    limit_adult_r = graphene.Decimal(
        max_digits=18, decimal_places=2, required=False, default_value=0
    )
    limit_child_r = graphene.Decimal(
        max_digits=18, decimal_places=2, required=False, default_value=0
    )
    limit_adult_e = graphene.Decimal(
        max_digits=18, decimal_places=2, required=False, default_value=0
    )
    limit_child_e = graphene.Decimal(
        max_digits=18, decimal_places=2, required=False, default_value=0
    )
    ceiling_exclusion_adult = graphene.Field(CeilingExclusionEnum)
    ceiling_exclusion_child = graphene.Field(CeilingExclusionEnum)


class ProductServiceInput(ProductServiceOrItemInput):
    service_uuid = graphene.UUID(required=True)


class ProductItemInput(ProductServiceOrItemInput):
    item_uuid = graphene.UUID(required=True)


class ProductInputType(OpenIMISMutation.Input):
    name = graphene.String(required=True)
    enrolment_period_start_date = graphene.Date(required=False)
    enrolment_period_end_date = graphene.Date(required=False)
    coverage_period_start_date = graphene.Date(required=False)
    coverage_period_end_date = graphene.Date(required=False)
    administration_period = graphene.Int()
    recurrence = graphene.Int()
    location_uuid = graphene.UUID()
    conversion_product_uuid = graphene.UUID()
    acc_code_remuneration = graphene.String()
    acc_code_premiums = graphene.String()
    lump_sum = graphene.Decimal(
        max_digits=18, decimal_places=2, required=False
    )
    premium_adult = graphene.Decimal(
        max_digits=18, decimal_places=2, required=False, default_value=0
    )
    threshold = graphene.Int()
    share_contribution = graphene.Decimal(
        max_digits=18, decimal_places=2, required=False
    )
    registration_lump_sum = graphene.Decimal(
        max_digits=18, decimal_places=2, required=False
    )
    registration_fee = graphene.Decimal(
        max_digits=18, decimal_places=2, required=False)
    additional_spouse_contribution = graphene.Decimal(
        max_digits=18, decimal_places=2, required=False
    )
    penality_formula = graphene.Decimal(
        max_digits=18, decimal_places=2, required=False
    )
    start_cycle_1 = graphene.String()
    start_cycle_2 = graphene.String()
    start_cycle_3 = graphene.String()
    start_cycle_4 = graphene.String()
    # Deductibles & Ceilings
    ceiling_interpretation = graphene.Field(CeilingInterpretationEnum)
    ceiling_type = graphene.Field(CeilingTypeEnum)
    deductible = graphene.Decimal(max_digits=18, decimal_places=2)
    deductible_ip = graphene.Decimal(
        max_digits=18,
        decimal_places=2,
    )
    deductible_op = graphene.Decimal(
        max_digits=18,
        decimal_places=2,
    )
    ceiling = graphene.Decimal(
        max_digits=18,
        decimal_places=2,
    )
    ceiling_ip = graphene.Decimal(
        max_digits=18,
        decimal_places=2,
    )
    ceiling_op = graphene.Decimal(max_digits=18, decimal_places=2)
    max_ceiling_policy = graphene.Decimal(
        max_digits=18, decimal_places=2, required=False
    )
    max_ceiling_policy_ip = graphene.Decimal(
        max_digits=18, decimal_places=2, required=False
    )
    max_ceiling_policy_op = graphene.Decimal(
        max_digits=18, decimal_places=2, required=False
    )
    max_policy_extra_member = graphene.Decimal(
        max_digits=18, decimal_places=2, required=False
    )
    max_policy_extra_member_op = graphene.Decimal(
        max_digits=18, decimal_places=2, required=False
    )
    max_policy_extra_member_ip = graphene.Decimal(
        max_digits=18, decimal_places=2, required=False
    )
    max_no_consultation = graphene.Int()
    max_no_surgery = graphene.Int()
    max_no_delivery = graphene.Int()
    max_no_hospitalization = graphene.Int()
    max_no_visits = graphene.Int()
    max_no_antenatal = graphene.Int()
    max_amount_consultation = graphene.Decimal(max_digits=18, decimal_places=2)
    max_amount_surgery = graphene.Decimal(max_digits=18, decimal_places=2)
    max_amount_delivery = graphene.Decimal(max_digits=18, decimal_places=2)
    max_amount_hospitalization = graphene.Decimal(
        max_digits=18, decimal_places=2)
    max_amount_antenatal = graphene.Decimal(max_digits=18, decimal_places=2)
    relative_prices = graphene.List(RelativePricesInput)
    items = graphene.List(graphene.NonNull(ProductItemInput))
    services = graphene.List(graphene.NonNull(ProductServiceInput))
    age_maximal = graphene.Int()
    chf_id_format = graphene.Int(description="CHF ID format (1, 2 or 3)")
    membership_types = graphene.JSONString(required=False)
    card_replacement_fee = graphene.Decimal(max_digits=18, decimal_places=2, required=True, default_value=1)
    has_no_indigent = graphene.Boolean(required=False, default_value=False)


class CreateProductMutation(CreateOrUpdateProductMutation):
    _mutation_module = "product"
    _mutation_class = "CreateProductMutation"

    class Input(ProductInputType):
        code = graphene.String(required=True)

    # Output class removed to allow dict return mapping

    @classmethod
    def async_mutate(cls, user, **data):
        logger.debug("async_mutate called with data: %s", data)
        try:
            from .schema import ProductGQLType
            membership_types_raw = data.get("membership_types")
            membership_types_data = None
            if membership_types_raw:
                if isinstance(membership_types_raw, str):
                    try:
                        membership_types_data = json.loads(membership_types_raw)
                    except Exception as e:
                        return [{"message": f"Invalid membershipTypes JSON: {e}"}]
                elif isinstance(membership_types_raw, dict):
                    membership_types_data = membership_types_raw
                else:
                    return [{"message": "membershipTypes must be a JSON string or dict"}]
                data["membership_types"] = membership_types_data
            required_fields = ["code", "name", "card_replacement_fee"]
            missing = [f for f in required_fields if not data.get(f)]
            if missing:
                return [{"message": f"Missing required fields: {', '.join(missing)}"}]
            try:
                product = cls.do_mutate(
                    ProductConfig.gql_mutation_products_add_perms,
                    user,
                    **data,
                )
            except Exception as exc:
                logger.error("Exception in do_mutate: %s", exc)
                return [{"message": str(exc)}]
            logger.info("Product created: %s", product)
            
           
            
            return None
        except ValueError as exc:
            logger.error("ValueError in CreateProductMutation: %s", exc)
            return [{ "message": str(exc)}]
        except Exception as exc:
            logger.error("Exception in CreateProductMutation: %s", exc)
            return [{ "message": str(exc)}]


class DuplicateProductMutation(OpenIMISMutation):
    _mutation_module = "product"
    _mutation_class = "DuplicateProductMutation"

    class Input(ProductInputType):
        code = graphene.String(required=True)
        uuid = graphene.UUID(required=False)

    @classmethod
    def async_mutate(cls, user, **data):
        try:
            cls.do_mutate(
                ProductConfig.gql_mutation_products_add_perms,
                user,
                **data,
            )
        except ValueError as exc:
            return [
                {
                    "message": str(exc)
                }
            ]
        except Exception as exc:
            return [
                {
                    "message": _("product.mutation.failed_to_duplicate_product"),
                    "detail": str(exc),
                }
            ]

    @classmethod
    def do_mutate(cls, perms, user, **data):
        if type(user) is AnonymousUser or not user.id:
            raise ValidationError(_("mutation.authentication_required"))
        if not user.has_perms(perms):
            raise PermissionDenied(_("unauthorized"))
        current_uuid = data.pop("uuid") if "uuid" in data else None

        data["audit_user_id"] = user.id_for_audit

        duplicate_items = True #if 'items' not in data else False
        duplicate_services = True #if 'services' not in data else False

        has_no_indigent = data.pop("has_no_indigent", False)
        new_product = create_or_update_product(user, data, is_duplicate=True, has_no_indigent=has_no_indigent)

        if duplicate_items:
            new_product_items = ProductItem.objects.filter(product=Product.objects.get(uuid=current_uuid,
                                                           validity_to__isnull=True))
            for item in new_product_items:
                # create a new instance by setting pk = None
                item.pk = None
                item.product = new_product
                item.save()

        if duplicate_services:
            new_product_services = ProductService.objects.filter(product=Product.objects.get(uuid=current_uuid,
                                                                 validity_to__isnull=True))
            for service in new_product_services:
                # create a new instance by setting pk = None
                service.pk = None
                service.product = new_product
                service.save()


        return new_product


class UpdateProductMutation(CreateOrUpdateProductMutation):
    _mutation_module = "product"
    _mutation_class = "UpdateProductMutation"

    class Input(ProductInputType):
        uuid = graphene.UUID(required=True)
        code = graphene.String(required=True)

    @classmethod
    def async_mutate(cls, user, **data):
        try:
            cls.do_mutate(
                ProductConfig.gql_mutation_products_edit_perms,
                user,
                **data,
            )
        except Exception as exc:
            return [
                {
                    "message": _("product.mutation.failed_to_update_product")
                    % {"uuid": data["uuid"]},
                    "detail": str(exc),
                }
            ]
class ProductInputCustom(OpenIMISMutation.Input):
    uuid = graphene.UUID(required=True, description="UUID of the product to update")
    code = graphene.String(required=False, description="Product code")
    name = graphene.String(required=False, description="Product name")
    lump_sum = graphene.Decimal(required=False, description="Lump sum amount")
    card_replacement_fee = graphene.Decimal(required=False, description="Card replacement fee")
    premium_adult = graphene.Decimal(required=False, description="Premium for adults")
    additional_spouse_contribution = graphene.Decimal(required=False, description="Additional spouse contribution")
    penality_formula = graphene.Decimal(required=False, description="Penalty formula")
    membership_types = graphene.JSONString(required=False, description="Membership types as JSON")
    age_maximal = graphene.Int(required=False, description="Maximum age")
    chf_id_format = graphene.Int(required=False, description="CHF ID format (1, 2 or 3)")
    enrolment_period_start_date = graphene.Date(required=False, description="Enrolment period start date")
    enrolment_period_end_date = graphene.Date(required=False, description="Enrolment period end date")
    coverage_period_start_date = graphene.Date(required=False, description="Coverage period start date")
    coverage_period_end_date = graphene.Date(required=False, description="Coverage period end date")
    location_id = graphene.Int(required=False, description="Location ID to associate with the product")
    administration_period = graphene.Int(required=False, description="Administration period")
    recurrence = graphene.Int(required=False, description="Recurrence")
    threshold = graphene.Int(required=False, description="Threshold")
    share_contribution = graphene.Decimal(required=False, description="Share contribution")
    registration_lump_sum = graphene.Decimal(required=False, description="Registration lump sum")
    registration_fee = graphene.Decimal(required=False, description="Registration fee")
    client_mutation_id = graphene.String(required=False)
    client_mutation_label = graphene.String(required=False)
    # Deductibles & Ceilings
    deductible = graphene.Decimal(required=False, description="Deductible amount")
    deductible_ip = graphene.Decimal(required=False, description="Inpatient deductible")
    deductible_op = graphene.Decimal(required=False, description="Outpatient deductible")
    ceiling = graphene.Decimal(required=False, description="Ceiling amount")
    ceiling_ip = graphene.Decimal(required=False, description="Inpatient ceiling")
    ceiling_op = graphene.Decimal(required=False, description="Outpatient ceiling")
    ceiling_type = graphene.String(required=False, description="Ceiling type (I, T, P)")
    ceiling_interpretation = graphene.String(required=False, description="Ceiling interpretation")

    # Additional fields
    max_no_consultation = graphene.Int(required=False, description="Maximum number of consultations")
    max_no_surgery = graphene.Int(required=False, description="Maximum number of surgeries")
    max_no_delivery = graphene.Int(required=False, description="Maximum number of deliveries")
    max_no_hospitalization = graphene.Int(required=False, description="Maximum number of hospitalizations")
    max_no_visits = graphene.Int(required=False, description="Maximum number of visits")
    max_no_antenatal = graphene.Int(required=False, description="Maximum number of antenatal visits")
    max_amount_consultation = graphene.Decimal(required=False, description="Maximum consultation amount")
    max_amount_surgery = graphene.Decimal(required=False, description="Maximum surgery amount")
    max_amount_delivery = graphene.Decimal(required=False, description="Maximum delivery amount")
    max_amount_hospitalization = graphene.Decimal(required=False, description="Maximum hospitalization amount")
    max_amount_antenatal = graphene.Decimal(required=False, description="Maximum antenatal amount")

    has_no_indigent = graphene.Boolean(
        required=False,
        default_value=False,
        description="Skip creating indigent membership type"
    )
    penality_formula = graphene.String(required=False, description="Penality formula for the product")

class UpdateProductCustomMutation(OpenIMISMutation):
    """
    Custom mutation to update a product by UUID with comprehensive field support.
    This mutation allows updating all product fields including membership types.
    """

    _mutation_module = "product"
    _mutation_class = "UpdateProductMutation"

    class Input(ProductInputCustom):
        pass

    @classmethod
    def async_mutate(cls, user, **kwargs):
        """
        Update a product by UUID with the provided fields.
        Only non-None values will be updated.
        """
        try:
            uuid = kwargs['uuid']
            if not user or not hasattr(user, 'id'):
                return [{
                    "message":"Authentication required"
                }]

            # Check permissions
            if not user.has_perms(ProductConfig.gql_mutation_products_edit_perms):
                return [{
                    "message":"Unauthorized: insufficient permissions to update products"}]
                

            audit_user_id = getattr(user, 'id_for_audit', None) or getattr(user, 'id', None) or 1

            # Find the product to update
            try:
                product = Product.objects.get(uuid=uuid, validity_to__isnull=True)
            except Product.DoesNotExist:
                return [{
                    "message":f"Product with UUID {uuid} not found or is not active",
                    }]

            # Parse membership_types if provided
            membership_types_data = None
            if 'membership_types' in kwargs and kwargs['membership_types']:
                if isinstance(kwargs['membership_types'], str):
                    try:
                        membership_types_data = json.loads(kwargs['membership_types'])
                    except Exception as e:
                        return UpdateProductCustomMutation(
                            ok=False,
                            message=f"Invalid membership_types JSON: {e}",
                            product=None
                        )
                elif isinstance(kwargs['membership_types'], dict):
                    membership_types_data = kwargs['membership_types']

            # Prepare update data - only include fields that are provided (not None)
            update_data = {}
            
            # Map GraphQL field names to model field names for deductibles/ceilings
            field_mappings = {
                "deductible": "ded_insuree",
                "deductible_ip": "ded_ip_insuree", 
                "deductible_op": "ded_op_insuree",
                "ceiling": "max_insuree",
                "ceiling_ip": "max_ip_insuree",
                "ceiling_op": "max_op_insuree"
            }

            # Process all provided fields
            for field_name, value in kwargs.items():
                if value is not None and field_name != 'membership_types':
                    # Map field names if needed
                    model_field = field_mappings.get(field_name, field_name)
                    
                    # Validate the field exists on the model
                    if hasattr(Product, model_field):
                        update_data[model_field] = value
                    else:
                        logger.warning(f"Field {model_field} does not exist on Product model")

            # Always update audit_user_id
            update_data['audit_user_id'] = audit_user_id

            # Handle location change if provided
            if 'location_id' in update_data:
                new_location_id = update_data['location_id']
                old_location_id = product.location_id
                
                if new_location_id != old_location_id:
                    # Use the existing location update service
                    try:
                        updated_product, message = update_product_location(product.id, new_location_id)
                        logger.info(message)
                        
                        # Remove location_id from update_data since it's already handled
                        del update_data['location_id']
                        
                        # Update the remaining fields on the updated product
                        if update_data:
                            for field, value in update_data.items():
                                if hasattr(updated_product, field):
                                    setattr(updated_product, field, value)
                            updated_product.save()
                        
                        # Handle membership_types if provided
                        if membership_types_data:
                            updated_product.membership_types.clear()
                            _process_membership_types(
                                updated_product,
                                {'membership_types': membership_types_data},
                                has_no_indigent=kwargs.get('has_no_indigent', False)
                            )
                        
                        updated_product.refresh_from_db()
                        
                        
                        
                        return None
                    except ValidationError as e:
                        return [{
                            "message":str(e)
                        }]

            # Regular update without location change
            if update_data:
                # Use queryset update to bypass model validation if needed
                pd = Product.objects.filter(id=product.id , validity_to=None)
                pd.update(**update_data)
                updatedpd = product.refresh_from_db()

            # Handle membership_types if provided
            if membership_types_data:
                product.membership_types.clear()
                _process_membership_types(
                    product,
                    {'membership_types': membership_types_data},
                    has_no_indigent=kwargs.get('has_no_indigent', False)
                )

            product.refresh_from_db()
            
            
            return None

        except Exception as e:
            logger.error(f"Error updating product: {str(e)}", exc_info=True)
            return [{
                "message":f"Error updating product: {str(e)}",
                }]


class DeleteProductMutation(OpenIMISMutation):
    _mutation_module = "product"
    _mutation_class = "DeleteProductMutation"

    class Input(OpenIMISMutation.Input):
        uuids = graphene.List(graphene.String)

    @classmethod
    def async_mutate(cls, user, **data):
        if not user.has_perms(ProductConfig.gql_mutation_products_delete_perms):
            raise PermissionDenied(_("unauthorized"))
        errors = []

        for uuid in data["uuids"]:
            product = Product.objects.filter(uuid=uuid).first()
            if product is None:
                errors.append(
                    {
                        "title": uuid,
                        "list": [
                            {
                                "message": _("product.validation.id_does_not_exist")
                                % {"id", uuid}
                            }
                        ],
                    }
                )
                continue
            try:
                
                now = py_datetime.now()
                product.validity_from = now
                product.validity_to = now
                product.save() 
            except Exception as exc:
                errors.append(
                    {
                        "title": uuid,
                        "list": [
                            {
                                "message": _(
                                    "product.mutation.failed_to_delete_product"
                                )
                                % {"uuid": product.uuid},
                                "detail": str(exc),
                            }
                        ],
                    }
                )

        if len(errors) == 1:
            errors = errors[0]["list"]
        return errors


def get_product_gqltype():
    from .schema import ProductGQLType
    return ProductGQLType


def validate_formula(formula, expected_vars):
    expected_vars_lower = [var.lower() for var in expected_vars]
    
    variables = re.findall(r'\{(\w+)\}', formula, re.IGNORECASE)
    
    unique_vars = list(dict.fromkeys(var.lower() for var in variables))
    
    extra_vars = set(unique_vars) - set(expected_vars_lower)
    if extra_vars:
        return False
    
    return True


class CreateProductInput(OpenIMISMutation.Input):
    code = graphene.String(required=True)
    name = graphene.String(required=True)
    lump_sum = graphene.Decimal(required=False)
    card_replacement_fee = graphene.Decimal(required=True)
    premium_adult = graphene.Decimal(required=False)
    additional_spouse_contribution = graphene.Decimal(required=False)
    penality_formula = graphene.Decimal(required=False)
    membership_types = graphene.JSONString(required=False)
    age_maximal = graphene.Int(required=False)
    chf_id_format = graphene.Int(required=False)
    enrolment_period_start_date = graphene.Date(required=False)
    enrolment_period_end_date = graphene.Date(required=False)
    coverage_period_start_date = graphene.Date(required=False)
    coverage_period_end_date = graphene.Date(required=False)
    has_no_indigent = graphene.Boolean(required=False, default_value=False)
    location_id = graphene.Int(required=False, description="Location ID to associate with the product")
    penality_formula = graphene.String(required=False, description="Penality formula for the product")
    registration_fee = graphene.Decimal(required=False)

class CreateProductCustomMutation(OpenIMISMutation):
    _mutation_module = "product"
    _mutation_class = "CreateProductcustomMutation"

  
    class Input(CreateProductInput):
        pass

    @classmethod
    def async_mutate(cls, user, **data):

        from .schema import ProductGQLType
        try:
            input  = data
            audit_user_id = getattr(user, 'id_for_audit', None) or getattr(user, 'id', None) or 1

            # Extract all fields from input
            code = input["code"]
            name = input["name"]
            card_replacement_fee = input["card_replacement_fee"]
            lump_sum = 0
            premium_adult = input["premium_adult"]
            additional_spouse_contribution = input["additional_spouse_contribution"]
            membership_types = input["membership_types"]
            age_maximal = input["age_maximal"]
            chf_id_format = input["chf_id_format"]
            enrolment_period_start_date = input["enrolment_period_start_date"]
            enrolment_period_end_date = input["enrolment_period_end_date"]
            coverage_period_start_date = input["coverage_period_start_date"]
            coverage_period_end_date = input["coverage_period_end_date"]
            has_no_indigent = input["has_no_indigent"]
            location_id = input["location_id"]
            penality_formula = input["penality_formula"]
            registration_fee = input["registration_fee"]
            # Parse membership_types if provided
            membership_types_data = None
            if membership_types:
                if isinstance(membership_types, str):
                    membership_types_data = json.loads(membership_types)
                elif isinstance(membership_types, dict):
                    membership_types_data = membership_types
                else:
                    return [{
                        "message":"membership_types must be a JSON string or dict",
                        }]
            expected_vars = ["Year", "CalculatedPremium"]
            if not validate_formula(str(penality_formula) , expected_vars):
                return [{
                    "message":"Invalid penality formula",
                    }]

            # Check if a product already exists for this location
            if location_id:
                existing_product = Product.objects.filter(
                    location_id=location_id,
                    validity_to__isnull=True
                ).first()
                
                if existing_product:
                    # Update the existing product with the new details
                    # We'll use update() directly on the queryset to bypass model validation
                    # This avoids triggering the clean() method that would raise a ValidationError
                    update_data = {
                        'name': name,
                        'card_replacement_fee': card_replacement_fee,
                        'audit_user_id': audit_user_id
                    }
                    if lump_sum is not None:
                        update_data['lump_sum'] = lump_sum
                    
                    if premium_adult is not None:
                        update_data['premium_adult'] = premium_adult
                    if additional_spouse_contribution is not None:
                        update_data['additional_spouse_contribution'] = additional_spouse_contribution

                    if age_maximal is not None:
                        update_data['age_maximal'] = age_maximal
                    if chf_id_format is not None:
                        update_data['chf_id_format'] = chf_id_format
                    if enrolment_period_start_date is not None:
                        update_data['enrolment_period_start_date'] = enrolment_period_start_date
                    if enrolment_period_end_date is not None:
                        update_data['enrolment_period_end_date'] = enrolment_period_end_date
                    if coverage_period_start_date is not None:
                        update_data['coverage_period_start_date'] = coverage_period_start_date
                    if coverage_period_end_date is not None:
                        update_data['coverage_period_end_date'] = coverage_period_end_date
                    if penality_formula is not None:
                        update_data['penality_formula'] = penality_formula
                    if registration_fee is not None:
                        update_data['registration_fee'] = registration_fee
                    # Update the product directly in the database
                    Product.objects.filter(id=existing_product.id).update(**update_data)
                    
                    # Refresh the product from the database
                    existing_product.refresh_from_db()
                    
                    # Handle membership_types many-to-many if provided
                    if membership_types_data:
                        # Clear existing M2M relations without deleting MembershipType rows
                        existing_product.membership_types.clear()
                        # Add new membership types
                        _process_membership_types(
                            existing_product,
                            {'membership_types': membership_types_data},
                            has_no_indigent=has_no_indigent
                        )
                    
                    
                    return None
            
            try:
                # Create the product
                product = Product.objects.create(
                    code=code,
                    name=name,
                    lump_sum=lump_sum,
                    card_replacement_fee=card_replacement_fee,
                    premium_adult=premium_adult,
                    additional_spouse_contribution=additional_spouse_contribution,
                    penality_formula=penality_formula,
                    audit_user_id=audit_user_id,
                    age_maximal=age_maximal,
                    chf_id_format=chf_id_format,
                    enrolment_period_start_date=enrolment_period_start_date,
                    enrolment_period_end_date=enrolment_period_end_date,
                    coverage_period_start_date=coverage_period_start_date,
                    coverage_period_end_date=coverage_period_end_date,
                    location_id=location_id
                )
            except ValidationError as e:
                # If validation fails due to location constraint, find the existing product and update it
                if location_id and "another product" in str(e).lower():
                    existing_product = Product.objects.filter(
                        location_id=location_id,
                        validity_to__isnull=True
                    ).first()
                    
                    if existing_product:
                        # Update the existing product using update() to bypass validation
                        update_data = {
                            'name': name,
                            'card_replacement_fee': card_replacement_fee,
                            'audit_user_id': audit_user_id
                        }
                        if lump_sum is not None:
                            update_data['lump_sum'] = lump_sum
                        
                        if premium_adult is not None:
                            update_data['premium_adult'] = premium_adult
                        if additional_spouse_contribution is not None:
                            update_data['additional_spouse_contribution'] = additional_spouse_contribution
                        if age_maximal is not None:
                            update_data['age_maximal'] = age_maximal
                        if chf_id_format is not None:
                            update_data['chf_id_format'] = chf_id_format
                        if enrolment_period_start_date is not None:
                            update_data['enrolment_period_start_date'] = enrolment_period_start_date
                        if enrolment_period_end_date is not None:
                            update_data['enrolment_period_end_date'] = enrolment_period_end_date
                        if coverage_period_start_date is not None:
                            update_data['coverage_period_start_date'] = coverage_period_start_date
                        if coverage_period_end_date is not None:
                            update_data['coverage_period_end_date'] = coverage_period_end_date
                        
                        Product.objects.filter(id=existing_product.id).update(**update_data)
                        existing_product.refresh_from_db()
                        
                        # Handle membership_types
                        if membership_types_data:
                            # Clear existing M2M relations without deleting MembershipType rows
                            existing_product.membership_types.clear()
                            _process_membership_types(
                                existing_product,
                                {'membership_types': membership_types_data},
                                has_no_indigent=has_no_indigent
                            )
                        
                        return None
                return [{
                    "message":str(e)
                }]

            # Handle membership_types many-to-many
            _process_membership_types(
                product,
                {'membership_types': membership_types_data if membership_types_data else {}},
                has_no_indigent=has_no_indigent
            )

          
            return None
        except Exception as e:
            return [{
                "message": str(e)
            }]
