import logging
from django.apps import apps
from core import datetime
from core import filter_validity
from core.utils import TimeUtils
from .models import Product
from model_clone.utils import create_copy_of_instance

from django.core.exceptions import ValidationError
from django.db import transaction

logger = logging.getLogger(__name__)


def save_product_history(product, items, services):
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
