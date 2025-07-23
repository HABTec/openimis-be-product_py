import datetime
import json
import logging
import traceback
from django.core.exceptions import ValidationError, PermissionDenied
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
def create_or_update_product(user, data, is_duplicate=False):
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

        # Validate required fields
        required_fields = ['code', 'name', 'lump_sum', 'card_replacement_fee']
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
            'enrolment_period_end_date', 'administration_period', 'recurrence', 'location_id',
            'conversion_product_id', 'acc_code_remuneration', 'acc_code_premiums', 'premium_adult',
            'threshold', 'share_contribution', 'registration_lump_sum', 'registration_fee',
            'start_cycle_1', 'start_cycle_2', 'start_cycle_3', 'start_cycle_4', 'ceiling_interpretation',
            'ceiling_type', 'ded_insuree', 'ded_ip_insuree', 'ded_op_insuree', 'max_insuree', 'max_ip_insuree',
            'max_op_insuree', 'max_ceiling_policy', 'max_ceiling_policy_ip', 'max_ceiling_policy_op',
            'max_policy_extra_member', 'max_policy_extra_member_op', 'max_policy_extra_member_ip',
            'max_no_consultation', 'max_no_surgery', 'max_no_delivery', 'max_no_hospitalization',
            'max_no_visits', 'max_no_antenatal', 'max_amount_consultation', 'max_amount_surgery',
            'max_amount_delivery', 'max_amount_hospitalization', 'max_amount_antenatal', 'age_maximal'
        ]
        
        # Filter and prepare product data
        product_data = {k: v for k, v in data.items() 
                       if k in product_fields and v is not None}

        # Generate a UUID if not provided
        if 'uuid' not in product_data:
            product_data['uuid'] = str(uuidlib.uuid4())

        # Start transaction
        with transaction.atomic():
            return _create_product_with_relations(user, product_data, is_duplicate)
            
    except Exception as e:
        error_msg = f"Error in create_or_update_product: {str(e)}"
        print(error_msg)
        raise Exception(error_msg)

def _create_product_with_relations(user, product_data, is_duplicate):
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
                'lump_sum': product_data.get('lump_sum', 0),
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
                'administration_period': product_data.get('administration_period'),
                'recurrence': product_data.get('recurrence'),
                'conversion_product_id': product_data.get('conversion_product_id'),
                'threshold': product_data.get('threshold'),
                'share_contribution': product_data.get('share_contribution'),
                'registration_lump_sum': product_data.get('registration_lump_sum'),
                'registration_fee': product_data.get('registration_fee'),
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
                'age_maximal': product_data.get('age_maximal')
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

def _process_membership_types(product, product_data):
    """Process and create membership types for the product."""
    if 'membership_types' not in product_data or not product_data['membership_types']:
        logger.info("No membership types provided")
        return
        
    membership_types_input = product_data['membership_types']
    if not isinstance(membership_types_input, dict):
        logger.warning("membership_types should be a dictionary")
        return
    
    region = membership_types_input.get('region')
    district = membership_types_input.get('district')
    levels = membership_types_input.get('levels', {})
    
    if not product or not hasattr(product, 'id') or not product.id:
        raise ValueError("Cannot process membership types: Product has no ID")
    
    # Ensure we have a valid audit user ID
    audit_user_id = getattr(product, 'audit_user_id', 1) or 1

    # Get valid MembershipType fields
    valid_fields = {f.name for f in MembershipType._meta.get_fields() if f.concrete and not f.many_to_many and not f.one_to_many}
    
    try:
        logger.info("Processing membership types for product %s", product.id)
        
        # Create a list to hold all membership types
        membership_types = []
        
        # Create urban membership types
        for idx, price in enumerate(levels.get('urban', []), start=1):
            if price is not None:  # Only create if price is not None
                try:
                    mt_data = {
                        'region': region,
                        'district': district,
                        'level_type': 'urban',
                        'level_index': idx,
                        'price': price,
                        'audit_user_id': audit_user_id
                    }
                    filtered_mt_data = {k: v for k, v in mt_data.items() if k in valid_fields}
                    membership_type = MembershipType.objects.create(**filtered_mt_data)
                    membership_types.append(membership_type)
                    logger.debug("Created urban membership type %s for product %s", membership_type.id, product.id)
                except Exception as e:
                    logger.error("Error creating urban membership type: %s", str(e))
                    raise
        
        # Create rural membership types
        for idx, price in enumerate(levels.get('rural', []), start=1):
            if price is not None:  # Only create if price is not None
                try:
                    mt_data = {
                        'region': region,
                        'district': district,
                        'level_type': 'rural',
                        'level_index': idx,
                        'price': price,
                        'audit_user_id': audit_user_id
                    }
                    filtered_mt_data = {k: v for k, v in mt_data.items() if k in valid_fields}
                    membership_type = MembershipType.objects.create(**filtered_mt_data)
                    membership_types.append(membership_type)
                    logger.debug("Created rural membership type %s for product %s", membership_type.id, product.id)
                except Exception as e:
                    logger.error("Error creating rural membership type: %s", str(e))
                    raise
        
        # Clear existing membership types and add the new ones in a single operation
        if membership_types:
            with transaction.atomic():
                product.membership_types.clear()
                product.membership_types.add(*membership_types)
                logger.info("Successfully updated %s membership types for product %s", len(membership_types), product.id)
        else:
            logger.info("No valid membership types to add")
            
    except Exception as e:
        error_msg = f"Error processing membership types: {str(e)}"
        logger.error(error_msg)
        raise Exception(error_msg) from e

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

        return create_or_update_product(user, data)


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
    administration_period = graphene.Int()
    recurrence = graphene.Int()
    location_uuid = graphene.UUID()
    conversion_product_uuid = graphene.UUID()
    acc_code_remuneration = graphene.String()
    acc_code_premiums = graphene.String()
    lump_sum = graphene.Decimal(
        max_digits=18, decimal_places=2, required=False, default_value=0
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
    membership_types = graphene.JSONString(required=False)
    card_replacement_fee = graphene.Decimal(max_digits=18, decimal_places=2, required=True, default_value=1)


class CreateProductMutation(CreateOrUpdateProductMutation):
    _mutation_module = "product"
    _mutation_class = "CreateProductMutation"

    class Input(ProductInputType):
        code = graphene.String(required=True)

    # Output class removed to allow dict return mapping

    @classmethod
    def async_mutate(cls, user, **data):
        print("async_mutate called with data:", data)
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

        new_product = create_or_update_product(user, data, is_duplicate=True)

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
        lump_sum = graphene.Decimal(required=True)
        card_replacement_fee = graphene.Decimal(required=True)
        premium_adult = graphene.Decimal(required=False)
        membership_types = graphene.JSONString(required=False)
        age_maximal = graphene.Int(required=False)  # Add this line
        # Add more fields as needed

    ok = graphene.Boolean()
    message = graphene.String()
    product = graphene.Field(lambda: __import__("product.schema", fromlist=["ProductGQLType"]).ProductGQLType)

    @classmethod
    def mutate(cls, root, info, code, name, lump_sum, card_replacement_fee, premium_adult=None, membership_types=None, age_maximal=None, **kwargs):
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
            product = Product.objects.create(
                code=code,
                name=name,
                lump_sum=lump_sum,
                card_replacement_fee=card_replacement_fee,
                premium_adult=premium_adult,
                audit_user_id=audit_user_id,
                age_maximal=age_maximal,  # Add this line
            )
            # Handle membership_types many-to-many
            if membership_types_data:
                _process_membership_types(product, {'membership_types': membership_types_data})
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
