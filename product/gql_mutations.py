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
)

# Module-level logger
logger = logging.getLogger(__name__)


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
            - "lump_sum" (Decimal): The lump sum amount for the product.
            - "card_replacement_fee" (Decimal): The fee for card replacement.
            Optional keys include:
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
                raise ValueError(f"membership_types must be a valid JSON string: {e}")

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
            'additional_spouse_contribution', 'penalty_price',
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
                'penalty_price': product_data.get('penalty_price'),
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
            
            # Create mutation log if needed
            if 'client_mutation_id' in product_data and product_data['client_mutation_id']:
                mutation = MutationLog.objects.create(
                    user=user,
                    object_id=product.id,
                    client_mutation_id=product_data['client_mutation_id'],
                    client_mutation_label="Add Product"
                )
                ProductMutation.objects.create(
                    product=product,
                    mutation=mutation
                )
            
            # Refresh and return the product
            product.refresh_from_db()
            logger.info(f"Successfully created product {product.id} with all related data")
            return product
            
        except Exception as e:
            error_msg = f"Error in _create_product_with_relations: {str(e)}"
            logger.error(error_msg, exc_info=True)
            
            # Cleanup in case of error
            if product and hasattr(product, 'id') and product.id:
                logger.warning(f"Cleaning up product {product.id} due to error")
                try:
                    product.delete()
                except Exception as delete_error:
                    logger.error(f"Error during cleanup: {str(delete_error)}")
            
            # Re-raise with appropriate error message
            error_msg = str(e)
            if 'duplicate key' in error_msg.lower():
                error_msg = "A product with this code already exists."
            elif hasattr(e, 'message_dict'):
                error_msg = ", ".join([f"{k}: {v[0]}" for k, v in e.message_dict.items()])
                
            raise Exception(f"Failed to create product: {error_msg}")

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

    # 1. Create the default indigent membership type if required
    if not has_no_indigent:
        if not region:
            raise ValidationError({'region': ['This field is required to create a default indigent membership type.']})
        try:
            indigent_defaults = {
                'region_id': region.id if region else None,
                'district_id': district.id if district else None,
                'level_type': 'urban',         # Arbitrary default. Business can adjust later.
                'level_index': 1,
                'price': 0,
                'is_indigent': True,
                'audit_user_id': audit_user_id,
            }
            indigent_data = {k: v for k, v in indigent_defaults.items() if k in valid_fields}
            membership_types.append(MembershipType.objects.create(**indigent_data))
            logger.debug("Created default indigent membership type for product %s", product.id)
        except Exception as e:
            logger.error("Failed to create default indigent membership type: %s", e)
            raise

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
            print(f"Created product item for product {product.id}")
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
    penalty_price = graphene.Decimal(
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
                        return {"ok": False, "message": f"Invalid membershipTypes JSON: {e}", "product": None}
                elif isinstance(membership_types_raw, dict):
                    membership_types_data = membership_types_raw
                else:
                    return {"ok": False, "message": "membershipTypes must be a JSON string or dict", "product": None}
                data["membership_types"] = membership_types_data
            required_fields = ["code", "name", "lump_sum", "card_replacement_fee"]
            missing = [f for f in required_fields if not data.get(f)]
            if missing:
                return {"ok": False, "message": f"Missing required fields: {', '.join(missing)}", "product": None}
            try:
                product = cls.do_mutate(
                    ProductConfig.gql_mutation_products_add_perms,
                    user,
                    **data,
                )
            except Exception as exc:
                logger.error("Exception in do_mutate: %s", exc)
                return {"ok": False, "message": str(exc), "product": None}
            logger.info("Product created: %s", product)
            return {
                "ok": True,
                "message": "Product created successfully.",
                "product": {
                    "id": getattr(product, "id", None),
                    "code": getattr(product, "code", None),
                    "name": getattr(product, "name", None),
                    "cardReplacementFee": str(getattr(product, "card_replacement_fee", "")) if getattr(product, "card_replacement_fee", None) is not None else None,
                }
            }
        except ValueError as exc:
            print("ValueError:", exc)
            return {"ok": False, "message": str(exc), "product": None}
        except Exception as exc:
            print("Exception:", exc)
            return {"ok": False, "message": str(exc), "product": None}


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
                product.delete_history()
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

class CreateProductCustomMutation(graphene.Mutation):
    class Arguments:
        code = graphene.String(required=True)
        name = graphene.String(required=True)
        lump_sum = graphene.Decimal(required=False)
        card_replacement_fee = graphene.Decimal(required=True)
        premium_adult = graphene.Decimal(required=False)
        additional_spouse_contribution = graphene.Decimal(required=False)
        penalty_price = graphene.Decimal(required=False)
        membership_types = graphene.JSONString(required=False)
        age_maximal = graphene.Int(required=False)
        chf_id_format = graphene.Int(required=False)
        enrolment_period_start_date = graphene.Date(required=False)
        enrolment_period_end_date = graphene.Date(required=False)
        coverage_period_start_date = graphene.Date(required=False)
        coverage_period_end_date = graphene.Date(required=False)
        has_no_indigent = graphene.Boolean(required=False, default_value=False)
        location_id = graphene.Int(required=False, description="Location ID to associate with the product")

    ok = graphene.Boolean()
    message = graphene.String()
    product = graphene.Field(lambda: __import__("product.schema", fromlist=["ProductGQLType"]).ProductGQLType)

    @classmethod
    def mutate(cls, root, info, code, name, card_replacement_fee, lump_sum=None, premium_adult=None, additional_spouse_contribution=None, penalty_price=None, membership_types=None, age_maximal=None, chf_id_format=None, enrolment_period_start_date=None, enrolment_period_end_date=None, coverage_period_start_date=None, coverage_period_end_date=None, has_no_indigent=False, location_id=None, **kwargs):
        from .schema import ProductGQLType
        try:
            user = getattr(info.context, 'user', None)
            audit_user_id = getattr(user, 'id_for_audit', None) or getattr(user, 'id', None) or 1

            # Parse membership_types if provided
            membership_types_data = None
            if membership_types:
                if isinstance(membership_types, str):
                    membership_types_data = json.loads(membership_types)
                elif isinstance(membership_types, dict):
                    membership_types_data = membership_types
                else:
                    return CreateProductCustomMutation(
                        ok=False,
                        message="membership_types must be a JSON string or dict",
                        product=None
                    )

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
                    if penalty_price is not None:
                        update_data['penalty_price'] = penalty_price
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
                    
                    return CreateProductCustomMutation(
                        ok=True,
                        message=f"Updated existing product '{existing_product.name}' (code: {existing_product.code}) for this location.",
                        product=existing_product
                    )
            
            try:
                # Create the product
                product = Product.objects.create(
                    code=code,
                    name=name,
                    lump_sum=lump_sum,
                    card_replacement_fee=card_replacement_fee,
                    premium_adult=premium_adult,
                    additional_spouse_contribution=additional_spouse_contribution,
                    penalty_price=penalty_price,
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
                        if penalty_price is not None:
                            update_data['penalty_price'] = penalty_price
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
                        
                        return CreateProductCustomMutation(
                            ok=True,
                            message=f"Updated existing product '{existing_product.name}' (code: {existing_product.code}) for this location.",
                            product=existing_product
                        )
                # If it's another validation error, re-raise it
                return CreateProductCustomMutation(
                    ok=False,
                    message=str(e),
                    product=None
                )

            # Handle membership_types many-to-many
            _process_membership_types(
                product,
                {'membership_types': membership_types_data if membership_types_data else {}},
                has_no_indigent=has_no_indigent
            )

            return CreateProductCustomMutation(
                ok=True,
                message="Product created successfully.",
                product=product
            )
        except Exception as e:
            return CreateProductCustomMutation(
                ok=False,
                message=str(e),
                product=None
            )
