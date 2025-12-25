import logging
from django.apps import apps
from core import datetime
from core import filter_validity
from core.utils import TimeUtils
from .models import Product, ProductItem, ProductService, ProductLaboratoryService, MembershipType
from model_clone.utils import create_copy_of_instance

from django.core.exceptions import ValidationError
from django.utils import timezone
from django.db import transaction
from django.db import models

logger = logging.getLogger(__name__)


def save_product_history(product):
    hist_id = product.save_history()
    product.relative_distributions.update(validity_to=TimeUtils.now())
    return hist_id


care_type_to_field = {
    "I": "period_rel_prices_ip",
    "O": "period_rel_prices_op",
    "B": "period_rel_prices",
}

periods_to_period_rel_prices = {1: "Y", 4: "Q", 12: "M"}


def set_product_relative_distribution(product, hist_id, relative_distributions,user):
    RelativeDistribution = apps.get_model(
        "claim_batch", "RelativeDistribution")
    if RelativeDistribution is None:
        logger.warning("RelativeDistribution does not exist.")
        return
    if hist_id:
        product.relative_distributions.update(validity_to=TimeUtils.now(),product_id=hist_id)
    product.period_rel_prices = None
    product.period_rel_prices_ip = None
    product.period_rel_prices_op = None

    if relative_distributions is None:
        return

    for distr in relative_distributions:
        if len(distr.periods) not in (1, 4, 12):
            raise ValueError("Number of periods can only be 1, 4 or 12")

        setattr(
            product,
            care_type_to_field[distr.care_type],
            periods_to_period_rel_prices[len(distr.periods)],
        )
        for idx, percent in enumerate(distr.periods):
            RelativeDistribution.objects.create(
                audit_user_id=user.id_for_audit,
                percent=percent,
                product=product,
                period=idx + 1,
                type=len(distr.periods),
                care_type=distr.care_type,
                validity_from=TimeUtils.now(),
            )


# TODO: This function can be refactored once we clean the ded_* & max_* columns in DB
def set_product_deductible_and_ceiling(
    product, ceiling_type, deductibles, ceilings, user
):
    if (deductibles.all and (deductibles.ip or deductibles.op)) or (
        ceilings.all and (ceilings.ip or ceilings.op)
    ):
        raise Exception(
            "Deductibles and ceilings cannot be set for in/out and all at the same time"
        )

    # Reset all fields
    field_names = {"T": "treatment", "I": "insuree", "P": "policy"}
    for type in ["T", "P", "I"]:
        setattr(
            product,
            f"ded_{field_names[type]}",
            deductibles.all if type == ceiling_type else 0,
        )
        setattr(
            product,
            f"ded_ip_{field_names[type]}",
            deductibles.ip if type == ceiling_type else 0,
        )
        setattr(
            product,
            f"ded_op_{field_names[type]}",
            deductibles.op if type == ceiling_type else 0,
        )
        setattr(
            product,
            f"max_{field_names[type]}",
            ceilings.all if type == ceiling_type else 0,
        )
        setattr(
            product,
            f"max_op_{field_names[type]}",
            ceilings.op if type == ceiling_type else 0,
        )
        setattr(
            product,
            f"max_ip_{field_names[type]}",
            ceilings.ip if type == ceiling_type else 0,
        )




def set_product_details(details_list, detail_model, hist_id, incoming, user):
    DetailModel = apps.get_model("medical", detail_model)
    if not DetailModel:
        logger.warning(f"medical.{detail_model} does not exist.")
        return
    copied = []
    update_time=TimeUtils.now()
    if incoming is None:
        #just save a new version of the items
        for  detail in details_list.filter(*filter_validity()):
            copied.append(create_copy_of_instance(detail, attrs={'pk':None, 'validity_from': update_time }))
            
    #update the old items/services
    if hist_id:    
        details_list.update(validity_to=update_time, product_id=hist_id)
    #save the copied after making the update
    for cpd in copied:
        cpd.save()
    if incoming is not  None:
        # Ensure there no duplicates
        seen_uuids = []
        for item in incoming:
                #for mutation payload
            uuid = item.pop(f"{detail_model.lower()}_uuid", None)
            item_id = item.pop(f"{detail_model.lower()}_id", None)
            item['audit_user_id']=user.id_for_audit
            item['validity_from']=update_time

            if item_id in seen_uuids or  uuid in seen_uuids:
                raise ValidationError(
                    f"'{uuid}' is already linked to the product.")
            
            seen_uuids.append(uuid or item_id)
            item[detail_model.lower()]=DetailModel.objects.get(id=item_id) if item_id is not None else DetailModel.objects.get(uuid=uuid)
            details_list.create(
                **item,
            )
 


def check_unique_code_product(code):
    if Product.objects.filter(code=code, validity_to__isnull=True).exists():
        return [{"message": "Product code %s already exists" % code}]
    return []


def update_product_location(product_id, new_location_id):
    """
    Update a product's location while enforcing the one-product-per-location constraint.
    If another product already exists for the new location, update that product with the
    details from the current product and deactivate the current product.
    
    Args:
        product_id: ID of the product to update
        new_location_id: ID of the new location to assign
        
    Returns:
        tuple: (updated_product, message)
            - updated_product: The product that was updated (may be different from original)
            - message: A message describing what happened
    
    Raises:
        ValidationError: If there's an issue with the update process
    """
    try:
        with transaction.atomic():
            # Get the current product
            current_product = Product.objects.get(id=product_id, validity_to__isnull=True)
            
            # Check if another product exists for the new location
            existing_products = Product.objects.filter(
                location_id=new_location_id, 
                validity_to__isnull=True
            ).exclude(id=product_id)
            
            if existing_products.exists():
                # Another product already exists for this location
                existing_product = existing_products.first()
                
                # Update the existing product with details from the current product
                # Preserve the existing product's code, name, and location
                preserved_fields = {
                    'code': existing_product.code,
                    'name': existing_product.name,
                    'location_id': existing_product.location_id,
                    'id': existing_product.id,
                    'uuid': existing_product.uuid
                }
                
                # Save membership types from current product to transfer later
                membership_types = list(current_product.membership_types.all())
                
                # Create a copy of current product's attributes
                update_fields = {}
                for field in [f.name for f in Product._meta.fields if f.name not in ['id', 'uuid', 'code', 'name', 'location_id']]:
                    update_fields[field] = getattr(current_product, field)
                
                # Update the existing product
                for field, value in update_fields.items():
                    setattr(existing_product, field, value)
                
                existing_product.save()
                
                # Transfer membership types to the existing product
                # First, clear existing membership types
                existing_product.membership_types.all().delete()
                
                # Then create new ones based on the current product's membership types
                for mt in membership_types:
                    MembershipType.objects.create(
                        region_id=mt.region_id,
                        district_id=mt.district_id,
                        level_type=mt.level_type,
                        level_index=mt.level_index,
                        price=mt.price,
                        is_indigent=mt.is_indigent
                    )
                
                # Deactivate the current product
                current_product.validity_to = TimeUtils.now()
                current_product.save()
                
                return existing_product, f"Updated existing product '{existing_product.name}' with details from '{current_product.name}' and deactivated the latter."
            else:
                # No other product exists for this location, simply update the current product
                current_product.location_id = new_location_id
                current_product.save()
                
                return current_product, f"Updated product '{current_product.name}' with new location."
                
    except Product.DoesNotExist:
        raise ValidationError(f"Product with ID {product_id} not found.")
    except Exception as e:
        logger.error(f"Error updating product location: {str(e)}", exc_info=True)
        raise ValidationError(f"Error updating product location: {str(e)}")


@transaction.atomic
def create_product_from_parent(
    parent_product_id: int,
    new_location_id: int,
    enrolment_start_date,
    enrolment_end_date,
    user,
    code: str = None,
    name: str = None,
):
    """
    Clone a product to a new location with new enrolment period and link it to the parent
    via Product.parent_product so that future changes on the parent propagate to the child.

    Args:
        parent_product_id: ID of the parent product to clone
        new_location_id: Location ID for the cloned product
        enrolment_start_date: date for the cloned product's enrolment start
        enrolment_end_date: date for the cloned product's enrolment end
        user: user performing the operation (must have id_for_audit)
        code: optional product code for the child; generates one if not provided
        name: optional product name for the child; defaults to parent name with suffix

    Returns:
        Product: the newly created child product

    Raises:
        ValidationError: if constraints are violated
    """
    parent = Product.objects.get(id=parent_product_id, validity_to__isnull=True)

    # Enforce one product per location (custom clean also enforces this)
    if Product.objects.filter(location_id=new_location_id, validity_to__isnull=True).exists():
        raise ValidationError("A product already exists for the specified location. Only one product per location is allowed.")

    child = Product()
    # Copy fields except identifiers and child-specific overrides
    exclude = {
        'id','uuid','code','name','location_id','location',
        'enrolment_period_start_date','enrolment_period_end_date',
        'audit_user_id','parent_product','parent_product_id'
    }
    for f in Product._meta.fields:
        if f.name in exclude:
            continue
        setattr(child, f.name, getattr(parent, f.name))

    # Set overrides
    child.audit_user_id = user.id_for_audit
    child.parent_product = parent
    child.location_id = new_location_id
    child.enrolment_period_start_date = enrolment_start_date
    child.enrolment_period_end_date = enrolment_end_date

    # Set code and name
    if not name:
        name = f"{parent.name} - L{new_location_id}"
    child.name = name

    if not code:
        # derive a code within 8 chars: parent.code + last 2 digits of location id
        suffix = str(new_location_id)[-2:]
        base = (parent.code or "P")[:6]
        code = f"{base}{suffix}"
        # ensure uniqueness if needed
        idx = 1
        while Product.objects.filter(code=code, validity_to__isnull=True).exists():
            base = base[:-1] if len(base) > 1 else base
            code = f"{base}{suffix}{idx}"
            code = code[:8]
            idx += 1
    child.code = code

    # Save child first to get PK
    child.save()

    # Copy membership types (M2M copy)
    child.membership_types.set(parent.membership_types.all())

    # Copy items
    for pi in parent.items.all():
        ProductItem.objects.create(
            audit_user_id=user.id_for_audit,
            product=child,
            item=pi.item,
            price_origin=pi.price_origin,
            limitation_type=pi.limitation_type,
            limitation_type_r=pi.limitation_type_r,
            limitation_type_e=pi.limitation_type_e,
            waiting_period_adult=pi.waiting_period_adult,
            waiting_period_child=pi.waiting_period_child,
            limit_no_adult=pi.limit_no_adult,
            limit_no_child=pi.limit_no_child,
            limit_adult=pi.limit_adult,
            limit_child=pi.limit_child,
            limit_adult_r=pi.limit_adult_r,
            limit_adult_e=pi.limit_adult_e,
            limit_child_r=pi.limit_child_r,
            limit_child_e=pi.limit_child_e,
            ceiling_exclusion_adult=pi.ceiling_exclusion_adult,
            ceiling_exclusion_child=pi.ceiling_exclusion_child,
        )

    # Copy services
    for ps in parent.services.all():
        ProductService.objects.create(
            audit_user_id=user.id_for_audit,
            product=child,
            service=ps.service,
            price_origin=ps.price_origin,
            limit_adult=ps.limit_adult,
            limit_child=ps.limit_child,
            waiting_period_adult=ps.waiting_period_adult,
            waiting_period_child=ps.waiting_period_child,
            limit_no_adult=ps.limit_no_adult,
            limit_no_child=ps.limit_no_child,
            limitation_type=ps.limitation_type,
            limitation_type_r=ps.limitation_type_r,
            limitation_type_e=ps.limitation_type_e,
            limit_adult_r=ps.limit_adult_r,
            limit_adult_e=ps.limit_adult_e,
            limit_child_r=ps.limit_child_r,
            limit_child_e=ps.limit_child_e,
            ceiling_exclusion_adult=ps.ceiling_exclusion_adult,
            ceiling_exclusion_child=ps.ceiling_exclusion_child,
        )

    for pls in parent.lab_services.all():
        ProductLaboratoryService.objects.create(
            audit_user_id=user.id_for_audit,
            product=child,
            lab_service=pls.lab_service,
            price_origin=pls.price_origin,
            limit_adult=pls.limit_adult,
            limit_child=pls.limit_child,
            waiting_period_adult=pls.waiting_period_adult,
            waiting_period_child=pls.waiting_period_child,
            limit_no_adult=pls.limit_no_adult,
            limit_no_child=pls.limit_no_child,
            limitation_type=pls.limitation_type,
            limitation_type_r=pls.limitation_type_r,
            limitation_type_e=pls.limitation_type_e,
            limit_adult_r=pls.limit_adult_r,
            limit_adult_e=pls.limit_adult_e,
            limit_child_r=pls.limit_child_r,
            limit_child_e=pls.limit_child_e,
            ceiling_exclusion_adult=pls.ceiling_exclusion_adult,
            ceiling_exclusion_child=pls.ceiling_exclusion_child,
        )

    return child


def get_products_active_now_in_enrolment(current_date=None):
    """
    Return products that are currently valid and whose enrolment period
    includes the current date.

    Args:
        current_date (date, optional): The date to check against. Defaults to
            timezone.localdate().

    Returns:
        QuerySet[Product]: Products with validity_to is null and
            enrolment_period_start_date <= current_date <= enrolment_period_end_date.
    """
    if current_date is None:
        # Use now().date() to avoid calling localtime() on naive datetimes
        current_date = timezone.now().date()
    return Product.objects.filter(
        validity_to__isnull=True,
    ).filter(
        models.Q(enrolment_period_start_date__isnull=True) | models.Q(enrolment_period_start_date__lte=current_date)
    ).filter(
        models.Q(enrolment_period_end_date__isnull=True) | models.Q(enrolment_period_end_date__gte=current_date)
    )


@transaction.atomic
def update_product_fields(product_uuid, update_data, user):
    """
    Update a product's fields by UUID.
    
    Args:
        product_uuid (str): UUID of the product to update
        update_data (dict): Dictionary of fields to update
        user: User performing the update
        
    Returns:
        Product: Updated product instance
        
    Raises:
        ValidationError: If product not found or validation fails
    """
    try:
        product = Product.objects.get(uuid=product_uuid, validity_to__isnull=True)
    except Product.DoesNotExist:
        raise ValidationError(f"Product with UUID {product_uuid} not found or is not active")
    
    # Set audit user
    audit_user_id = getattr(user, 'id_for_audit', None) or getattr(user, 'id', None) or 1
    update_data['audit_user_id'] = audit_user_id
    
    # Update the product
    for field, value in update_data.items():
        if hasattr(product, field):
            setattr(product, field, value)
    
    product.save()
    return product
