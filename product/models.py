import uuid
from django.db.models.deletion import CASCADE
from django.utils.translation import gettext_lazy
from django.db import models
from core.models import VersionedModel, ObjectMutation, UUIDModel, MutationLog
from django.core.exceptions import ValidationError
from django.db.models.signals import post_save
from django.dispatch import receiver
from enum import Enum


class Product(VersionedModel):
    id = models.AutoField(db_column="ProdID", primary_key=True)
    uuid = models.CharField(
        db_column="ProdUUID", max_length=36, default=uuid.uuid4, unique=True
    )
    code = models.CharField(db_column="ProductCode", max_length=8)
    name = models.CharField(db_column="ProductName", max_length=100)
    location = models.ForeignKey(
        "location.Location",
        models.DO_NOTHING,
        db_column="LocationId",
        blank=True,
        null=True,
    )
    conversion_product = models.ForeignKey(
        "self", models.DO_NOTHING, db_column="ConversionProdID", blank=True, null=True
    )
    # Optional link to a parent product. When set, the child product inherits
    # most attributes and related items/services from the parent. Changes on the
    # parent will be propagated to children via a post_save signal, while
    # preserving child's own location and enrolment dates.
    parent_product = models.ForeignKey(
        "self",
        models.DO_NOTHING,
        db_column="ParentProdID",
        blank=True,
        null=True,
        related_name="children",
    )
    administration_period = models.IntegerField(
        db_column="AdministrationPeriod", blank=True, null=True
    )
    lump_sum = models.DecimalField(
        db_column="LumpSum", max_digits=18, decimal_places=2, blank=True, null=True
    )
    threshold = models.IntegerField(db_column="Threshold", blank=True, null=True)
    recurrence = models.IntegerField(db_column="Recurrence", blank=True, null=True)
    premium_adult = models.DecimalField(
        db_column="PremiumAdult", max_digits=18, decimal_places=2, blank=True, null=True
    )
    ded_insuree = models.DecimalField(
        db_column="DedInsuree", max_digits=18, decimal_places=2, blank=True, null=True
    )
    ded_op_insuree = models.DecimalField(
        db_column="DedOPInsuree", max_digits=18, decimal_places=2, blank=True, null=True
    )
    ded_ip_insuree = models.DecimalField(
        db_column="DedIPInsuree", max_digits=18, decimal_places=2, blank=True, null=True
    )
    max_insuree = models.DecimalField(
        db_column="MaxInsuree", max_digits=18, decimal_places=2, blank=True, null=True
    )
    max_op_insuree = models.DecimalField(
        db_column="MaxOPInsuree", max_digits=18, decimal_places=2, blank=True, null=True
    )
    max_ip_insuree = models.DecimalField(
        db_column="MaxIPInsuree", max_digits=18, decimal_places=2, blank=True, null=True
    )
    period_rel_prices = models.CharField(
        db_column="PeriodRelPrices", max_length=1, blank=True, null=True
    )
    period_rel_prices_op = models.CharField(
        db_column="PeriodRelPricesOP", max_length=1, blank=True, null=True
    )
    period_rel_prices_ip = models.CharField(
        db_column="PeriodRelPricesIP", max_length=1, blank=True, null=True
    )
    acc_code_premiums = models.CharField(
        db_column="AccCodePremiums", max_length=25, blank=True, null=True
    )
    acc_code_remuneration = models.CharField(
        db_column="AccCodeRemuneration", max_length=25, blank=True, null=True
    )
    ded_treatment = models.DecimalField(
        db_column="DedTreatment", max_digits=18, decimal_places=2, blank=True, null=True
    )
    ded_op_treatment = models.DecimalField(
        db_column="DedOPTreatment",
        max_digits=18,
        decimal_places=2,
        blank=True,
        null=True,
    )
    ded_ip_treatment = models.DecimalField(
        db_column="DedIPTreatment",
        max_digits=18,
        decimal_places=2,
        blank=True,
        null=True,
    )
    max_treatment = models.DecimalField(
        db_column="MaxTreatment", max_digits=18, decimal_places=2, blank=True, null=True
    )
    max_op_treatment = models.DecimalField(
        db_column="MaxOPTreatment",
        max_digits=18,
        decimal_places=2,
        blank=True,
        null=True,
    )
    max_ip_treatment = models.DecimalField(
        db_column="MaxIPTreatment",
        max_digits=18,
        decimal_places=2,
        blank=True,
        null=True,
    )
    ded_policy = models.DecimalField(
        db_column="DedPolicy", max_digits=18, decimal_places=2, blank=True, null=True
    )
    ded_op_policy = models.DecimalField(
        db_column="DedOPPolicy", max_digits=18, decimal_places=2, blank=True, null=True
    )
    ded_ip_policy = models.DecimalField(
        db_column="DedIPPolicy", max_digits=18, decimal_places=2, blank=True, null=True
    )
    max_policy = models.DecimalField(
        db_column="MaxPolicy", max_digits=18, decimal_places=2, blank=True, null=True
    )
    max_op_policy = models.DecimalField(
        db_column="MaxOPPolicy", max_digits=18, decimal_places=2, blank=True, null=True
    )
    max_ip_policy = models.DecimalField(
        db_column="MaxIPPolicy", max_digits=18, decimal_places=2, blank=True, null=True
    )
    audit_user_id = models.IntegerField(db_column="AuditUserID")
    registration_lump_sum = models.DecimalField(
        db_column="RegistrationLumpSum",
        max_digits=18,
        decimal_places=2,
        blank=True,
        null=True,
    )
    registration_fee = models.DecimalField(
        db_column="RegistrationFee",
        max_digits=18,
        decimal_places=2,
        blank=True,
        null=True,
    )
    additional_spouse_contribution = models.DecimalField(
        db_column="AdditionalSpouseContribution",
        max_digits=18,
        decimal_places=2,
        blank=True,
        null=True,
    )
    penalty_price = models.DecimalField(
        db_column="PenaltyPrice",
        max_digits=18,
        decimal_places=2,
        blank=True,
        null=True,
    )
    start_cycle_1 = models.CharField(
        db_column="StartCycle1", max_length=5, blank=True, null=True
    )
    start_cycle_2 = models.CharField(
        db_column="StartCycle2", max_length=5, blank=True, null=True
    )
    start_cycle_3 = models.CharField(
        db_column="StartCycle3", max_length=5, blank=True, null=True
    )
    start_cycle_4 = models.CharField(
        db_column="StartCycle4", max_length=5, blank=True, null=True
    )
    max_no_consultation = models.IntegerField(
        db_column="MaxNoConsultation", blank=True, null=True
    )
    max_no_surgery = models.IntegerField(
        db_column="MaxNoSurgery", blank=True, null=True
    )
    max_no_delivery = models.IntegerField(
        db_column="MaxNoDelivery", blank=True, null=True
    )
    max_no_hospitalization = models.IntegerField(
        db_column="MaxNoHospitalizaion", blank=True, null=True
    )
    max_no_visits = models.IntegerField(db_column="MaxNoVisits", blank=True, null=True)
    max_amount_consultation = models.DecimalField(
        db_column="MaxAmountConsultation",
        max_digits=18,
        decimal_places=2,
        blank=True,
        null=True,
    )
    max_amount_surgery = models.DecimalField(
        db_column="MaxAmountSurgery",
        max_digits=18,
        decimal_places=2,
        blank=True,
        null=True,
    )
    max_amount_delivery = models.DecimalField(
        db_column="MaxAmountDelivery",
        max_digits=18,
        decimal_places=2,
        blank=True,
        null=True,
    )
    max_amount_hospitalization = models.DecimalField(
        db_column="MaxAmountHospitalization",
        max_digits=18,
        decimal_places=2,
        blank=True,
        null=True,
    )
    share_contribution = models.DecimalField(
        db_column="ShareContribution",
        max_digits=5,
        decimal_places=2,
        blank=True,
        null=True,
    )
    max_policy_extra_member = models.DecimalField(
        db_column="MaxPolicyExtraMember",
        max_digits=18,
        decimal_places=2,
        blank=True,
        null=True,
    )
    max_policy_extra_member_ip = models.DecimalField(
        db_column="MaxPolicyExtraMemberIP",
        max_digits=18,
        decimal_places=2,
        blank=True,
        null=True,
    )
    max_policy_extra_member_op = models.DecimalField(
        db_column="MaxPolicyExtraMemberOP",
        max_digits=18,
        decimal_places=2,
        blank=True,
        null=True,
    )
    max_ceiling_policy = models.DecimalField(
        db_column="MaxCeilingPolicy",
        max_digits=18,
        decimal_places=2,
        blank=True,
        null=True,
    )
    max_ceiling_policy_ip = models.DecimalField(
        db_column="MaxCeilingPolicyIP",
        max_digits=18,
        decimal_places=2,
        blank=True,
        null=True,
    )
    max_ceiling_policy_op = models.DecimalField(
        db_column="MaxCeilingPolicyOP",
        max_digits=18,
        decimal_places=2,
        blank=True,
        null=True,
    )
    max_amount_antenatal = models.DecimalField(
        db_column="MaxAmountAntenatal",
        max_digits=18,
        decimal_places=2,
        blank=True,
        null=True,
    )
    ceiling_type = models.CharField(
        max_length=1,
        db_column="CeilingType",
        blank=True,
        null=True,
        choices=(
            ("I", gettext_lazy("INSUREE")),
            ("T", gettext_lazy("TREATMENT")),
            ("P", gettext_lazy("POLICY")),
        ),
    )
    max_no_antenatal = models.IntegerField(
        db_column="MaxNoAntenatal", blank=True, null=True
    )
    ceiling_interpretation = models.CharField(
        max_length=1,
        db_column="CeilingInterpretation",
        blank=True,
        null=True,
        choices=(
            ("I", gettext_lazy("Claim Type")),
            ("H", gettext_lazy("Health Facility Type")),
        ),
    )
    capitation_level_1 = models.CharField(
        db_column="Level1", max_length=1, blank=True, null=True
    )
    capitation_sublevel_1 = models.ForeignKey(
        "location.HealthFacilitySubLevel",
        models.DO_NOTHING,
        db_column="Sublevel1",
        blank=True,
        null=True,
        related_name="+",
    )
    capitation_level_2 = models.CharField(
        db_column="Level2", max_length=1, blank=True, null=True
    )
    capitation_sublevel_2 = models.ForeignKey(
        "location.HealthFacilitySubLevel",
        models.DO_NOTHING,
        db_column="Sublevel2",
        blank=True,
        null=True,
        related_name="+",
    )
    capitation_level_3 = models.CharField(
        db_column="Level3", max_length=1, blank=True, null=True
    )
    capitation_sublevel_3 = models.ForeignKey(
        "location.HealthFacilitySubLevel",
        models.DO_NOTHING,
        db_column="Sublevel3",
        blank=True,
        null=True,
        related_name="+",
    )
    capitation_level_4 = models.CharField(
        db_column="Level4", max_length=1, blank=True, null=True
    )
    capitation_sublevel_4 = models.ForeignKey(
        "location.HealthFacilitySubLevel",
        models.DO_NOTHING,
        db_column="Sublevel4",
        blank=True,
        null=True,
        related_name="+",
    )
    weight_population = models.DecimalField(
        db_column="WeightPopulation",
        max_digits=5,
        decimal_places=2,
        blank=True,
        null=True,
    )
    weight_nb_families = models.DecimalField(
        db_column="WeightNumberFamilies",
        max_digits=5,
        decimal_places=2,
        blank=True,
        null=True,
    )
    weight_insured_population = models.DecimalField(
        db_column="WeightInsuredPopulation",
        max_digits=5,
        decimal_places=2,
        blank=True,
        null=True,
    )
    weight_nb_insured_families = models.DecimalField(
        db_column="WeightNumberInsuredFamilies",
        max_digits=5,
        decimal_places=2,
        blank=True,
        null=True,
    )
    weight_nb_visits = models.DecimalField(
        db_column="WeightNumberVisits",
        max_digits=5,
        decimal_places=2,
        blank=True,
        null=True,
    )
    weight_adjusted_amount = models.DecimalField(
        db_column="WeightAdjustedAmount",
        max_digits=5,
        decimal_places=2,
        blank=True,
        null=True,
    )
    age_maximal = models.IntegerField(
        db_column="Max Age", blank=True, null=True
    )
    # CHF ID Format field (values 1, 2 or 3)
    CHF_ID_FORMAT_CHOICES = (
        (1, gettext_lazy("Format 1")),
        (2, gettext_lazy("Format 2")),
        (3, gettext_lazy("Format 3")),
    )
    chf_id_format = models.PositiveSmallIntegerField(
        db_column="CHFIDFormat",
        choices=CHF_ID_FORMAT_CHOICES,
        default=1,
        help_text="CHF ID format (1, 2 or 3)",
    )
    enrolment_period_start_date = models.DateField(blank=True, null=True)
    enrolment_period_end_date = models.DateField(blank=True, null=True)
    coverage_period_start_date = models.DateField(blank=True, null=True)
    coverage_period_end_date = models.DateField(blank=True, null=True)
    membership_types = models.ManyToManyField(
        'MembershipType',
        blank=True,
        related_name='products',
    )
    card_replacement_fee = models.DecimalField(
        db_column="CardReplacementFee",
        max_digits=18,
        decimal_places=2,
        default=1,
        null=False,
        blank=False,
        verbose_name="Card Replacement Fee",
    )
    penalityFormula = models.CharField(db_column="PenalityFormula", max_length=100 , blank=True, null=True)

    def fields_to_propagate_from_parent(self):
        """Return a list of field names to propagate from parent to child.
        Excludes identifiers and child-overridden fields.
        """
        exclude = {
            "id",
            "uuid",
            "code",
            "name",
            "location_id",
            "location",
            "enrolment_period_start_date",
            "enrolment_period_end_date",
            # Keep coverage dates in sync with parent by default (do not exclude)
            "parent_product_id",
            "parent_product",
            "conversion_product_id",
            "conversion_product",
            "audit_user_id",
        }
        return [
            f.name
            for f in self._meta.fields
            if f.name not in exclude
        ]

    def has_cycle(self):
        return (
            bool(self.start_cycle_1)
            or bool(self.start_cycle_2)
            or bool(self.start_cycle_3)
            or bool(self.start_cycle_4)
        )

    def clean(self):
        """Enforce one product per location (district and region)."""
        if self.location_id:
            # Check if there's another product with the same location
            existing_products = Product.objects.filter(location_id=self.location_id)
            
            # Exclude self when updating
            if self.id:
                existing_products = existing_products.exclude(id=self.id)
                
            if existing_products.exists():
                existing_product = existing_products.first()
                raise ValidationError(f"Another product '{existing_product.name}' (code: {existing_product.code}) already exists for this location. Only one product per location is allowed.")

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    class Meta:
        managed = True
        db_table = "tblProduct"

    CEILING_INTERPRETATION_HOSPITAL = "H"
    CEILING_INTERPRETATION_IN_PATIENT = "I"

    RELATIVE_PRICE_PERIOD_MONTH = "M"
    RELATIVE_PRICE_PERIOD_QUARTER = "Q"
    RELATIVE_PRICE_PERIOD_YEAR = "Y"


class ProductItemOrServiceManager(models.Manager):
    def filter(self, *args, **kwargs):
        keys = [x for x in kwargs if "itemsvc" in x]
        for key in keys:
            new_key = key.replace("itemsvc", self.model.model_prefix)
            kwargs[new_key] = kwargs.pop(key)
        return super(ProductItemOrServiceManager, self).filter(*args, **kwargs)


class ProductItemOrService:
    ORIGIN_PRICELIST = "P"
    ORIGIN_CLAIM = "O"
    ORIGIN_RELATIVE = "R"

    LIMIT_CO_INSURANCE = "C"
    LIMIT_FIXED_AMOUNT = "F"

    objects = ProductItemOrServiceManager()

    class Meta:
        abstract = True


LIMIT_CHOICES = (("F", gettext_lazy("Fixed")), ("C", gettext_lazy("Co-insurance")))


class ProductItem(VersionedModel, ProductItemOrService):
    id = models.AutoField(db_column="ProdItemID", primary_key=True)
    product = models.ForeignKey(
        Product, db_column="ProdID", on_delete=models.DO_NOTHING, related_name="items"
    )
    item = models.ForeignKey(
        "medical.Item",
        db_column="ItemID",
        on_delete=models.DO_NOTHING,
        related_name="items",
    )
    price_origin = models.CharField(
        db_column="PriceOrigin",
        max_length=1,
        null=True,
        blank=True,
        choices=(
            ("P", gettext_lazy("Schema Price")),
            ("O", gettext_lazy("Providers Own Price")),
            ("R", gettext_lazy("Relative Price")),
        ),
    )

    limitation_type = models.CharField(
        db_column="LimitationType",
        max_length=1,
        null=True,
        blank=True,
        choices=LIMIT_CHOICES,
    )
    limitation_type_r = models.CharField(
        db_column="LimitationTypeR",
        max_length=1,
        null=True,
        blank=True,
        choices=LIMIT_CHOICES,
    )
    limitation_type_e = models.CharField(
        db_column="LimitationTypeE",
        max_length=1,
        null=True,
        blank=True,
        choices=LIMIT_CHOICES,
    )

    waiting_period_adult = models.IntegerField(
        db_column="WaitingPeriodAdult", blank=True, null=True
    )
    waiting_period_child = models.IntegerField(
        db_column="WaitingPeriodChild", blank=True, null=True
    )
    limit_no_adult = models.IntegerField(
        db_column="LimitNoAdult", blank=True, null=True
    )
    limit_no_child = models.IntegerField(
        db_column="LimitNoChild", blank=True, null=True
    )
    limit_adult = models.DecimalField(
        db_column="LimitAdult",
        max_digits=18,
        decimal_places=2,
        blank=True,
        null=True,
    )
    limit_child = models.DecimalField(
        db_column="LimitChild", max_digits=18, decimal_places=2, blank=True, null=True
    )
    limit_adult_r = models.DecimalField(
        db_column="LimitAdultR", max_digits=18, decimal_places=2, blank=True, null=True
    )
    limit_adult_e = models.DecimalField(
        db_column="LimitAdultE", max_digits=18, decimal_places=2, blank=True, null=True
    )
    limit_child_r = models.DecimalField(
        db_column="LimitChildR", max_digits=18, decimal_places=2, blank=True, null=True
    )
    limit_child_e = models.DecimalField(
        db_column="LimitChildE", max_digits=18, decimal_places=2, blank=True, null=True
    )
    ceiling_exclusion_adult = models.CharField(
        db_column="CeilingExclusionAdult", max_length=1, null=True, blank=True
    )
    ceiling_exclusion_child = models.CharField(
        db_column="CeilingExclusionChild", max_length=1, null=True, blank=True
    )
    audit_user_id = models.IntegerField(db_column="AuditUserID")
    # rowid = models.TextField(db_column='RowID', blank=True, null=True) This field type is a guess.
    model_prefix = "item"
    objects = ProductItemOrServiceManager()

    class Meta:
        managed = True
        db_table = "tblProductItems"


class ProductService(VersionedModel, ProductItemOrService):
    id = models.AutoField(db_column="ProdServiceID", primary_key=True)
    product = models.ForeignKey(
        Product,
        db_column="ProdID",
        on_delete=models.DO_NOTHING,
        related_name="services",
    )
    service = models.ForeignKey(
        "medical.Service",
        db_column="ServiceID",
        on_delete=models.DO_NOTHING,
        related_name="services",
    )
    price_origin = models.CharField(
        db_column="PriceOrigin",
        max_length=1,
        choices=(
            ("P", gettext_lazy("Schema Price")),
            ("O", gettext_lazy("Providers Own Price")),
            ("R", gettext_lazy("Relative Price")),
        ),
    )
    limit_adult = models.DecimalField(
        db_column="LimitAdult", max_digits=18, decimal_places=2, blank=True, null=True
    )
    limit_child = models.DecimalField(
        db_column="LimitChild", max_digits=18, decimal_places=2, blank=True, null=True
    )
    waiting_period_adult = models.IntegerField(
        db_column="WaitingPeriodAdult", blank=True, null=True
    )
    waiting_period_child = models.IntegerField(
        db_column="WaitingPeriodChild", blank=True, null=True
    )
    limit_no_adult = models.IntegerField(
        db_column="LimitNoAdult", blank=True, null=True
    )
    limit_no_child = models.IntegerField(
        db_column="LimitNoChild", blank=True, null=True
    )
    limitation_type = models.CharField(
        db_column="LimitationType", max_length=1, choices=LIMIT_CHOICES
    )
    limitation_type_r = models.CharField(
        db_column="LimitationTypeR",
        max_length=1,
        null=True,
        blank=True,
        choices=LIMIT_CHOICES,
    )
    limitation_type_e = models.CharField(
        db_column="LimitationTypeE",
        max_length=1,
        null=True,
        blank=True,
        choices=LIMIT_CHOICES,
    )
    limit_adult_r = models.DecimalField(
        db_column="LimitAdultR", max_digits=18, decimal_places=2, blank=True, null=True
    )
    limit_adult_e = models.DecimalField(
        db_column="LimitAdultE", max_digits=18, decimal_places=2, blank=True, null=True
    )
    limit_child_r = models.DecimalField(
        db_column="LimitChildR", max_digits=18, decimal_places=2, blank=True, null=True
    )
    limit_child_e = models.DecimalField(
        db_column="LimitChildE", max_digits=18, decimal_places=2, blank=True, null=True
    )
    ceiling_exclusion_adult = models.CharField(
        db_column="CeilingExclusionAdult", max_length=1, null=True, blank=True
    )
    ceiling_exclusion_child = models.CharField(
        db_column="CeilingExclusionChild", max_length=1, null=True, blank=True
    )
    audit_user_id = models.IntegerField(db_column="AuditUserID")
    # rowid = models.TextField(db_column='RowID', blank=True, null=True) This field type is a guess.

    model_prefix = "service"
    objects = ProductItemOrServiceManager()

    class Meta:
        managed = True
        db_table = "tblProductServices"


class ProductMutation(UUIDModel, ObjectMutation):
    product = models.ForeignKey(Product, models.DO_NOTHING, related_name="+")
    mutation = models.ForeignKey(
        MutationLog, models.DO_NOTHING, related_name="products"
    )

    class Meta:
        managed = True
        db_table = "product_ProductMutation"


class MembershipType(models.Model):
    LEVEL_TYPE_CHOICES = [
        ("urban", "Urban"),
        ("rural", "Rural"),
    ]
    region_id = models.IntegerField(db_column="RegionID", null=True)
    district_id = models.IntegerField(db_column="DistrictID", null=True, blank=True)
    level_type = models.CharField(max_length=10, choices=LEVEL_TYPE_CHOICES)
    level_index = models.PositiveIntegerField()  # 1-based index
    price = models.DecimalField(max_digits=10, decimal_places=2)
    # Indicates whether this membership type is for indigent members. Defaults to False.
    is_indigent = models.BooleanField(db_column="IsIndigent", default=False)

    def clean(self):
        # Region is required for standard membership types but optional for indigent ones
        if not self.region_id and not self.is_indigent:
            raise ValidationError("region is required for non-indigent membership types")
        if self.level_type not in dict(self.LEVEL_TYPE_CHOICES):
            raise ValidationError("level_type must be 'urban' or 'rural'")
        if self.level_index < 1:
            raise ValidationError("level_index must be >= 1")
        if self.price < 0:
            raise ValidationError("price must be >= 0")

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    @classmethod
    def bulk_create_from_levels(cls, region, district, levels_dict):
        objs = []
        for level_type in ["urban", "rural"]:
            prices = levels_dict.get(level_type, [])
            for idx, price in enumerate(prices, 1):
                objs.append(cls(
                    region_id=region.id if region else None,
                    district_id=district.id if district else None,
                    level_type=level_type,
                    level_index=idx,
                    price=price
                ))
        return cls.objects.bulk_create(objs)


# Propagate selected changes from parent to all children
@receiver(post_save, sender=Product)
def propagate_product_changes_to_children(sender, instance: Product, created, **kwargs):
    # Do not propagate when saving a child to avoid loops
    if instance.parent_product_id:
        return
    # If there are no children, nothing to do
    children = getattr(instance, "children", None)
    if not children:
        return
    children_qs = children.all()
    if not children_qs.exists():
        return

    fields = instance.fields_to_propagate_from_parent()
    # Update scalar fields
    for child in children_qs:
        for fname in fields:
            setattr(child, fname, getattr(instance, fname))
        # Sync membership types to mirror the parent
        child.save(update_fields=fields)
        child.membership_types.set(instance.membership_types.all())
        # Sync items and services to mirror the parent definition
        # Replace child items/services with copies of parent's
        child.items.all().delete()
        for parent_item in instance.items.all():
            ProductItem.objects.create(
                audit_user_id=parent_item.audit_user_id,
                product=child,
                item=parent_item.item,
                price_origin=parent_item.price_origin,
                limitation_type=parent_item.limitation_type,
                limitation_type_r=parent_item.limitation_type_r,
                limitation_type_e=parent_item.limitation_type_e,
                waiting_period_adult=parent_item.waiting_period_adult,
                waiting_period_child=parent_item.waiting_period_child,
                limit_no_adult=parent_item.limit_no_adult,
                limit_no_child=parent_item.limit_no_child,
                limit_adult=parent_item.limit_adult,
                limit_child=parent_item.limit_child,
                limit_adult_r=parent_item.limit_adult_r,
                limit_adult_e=parent_item.limit_adult_e,
                limit_child_r=parent_item.limit_child_r,
                limit_child_e=parent_item.limit_child_e,
                ceiling_exclusion_adult=parent_item.ceiling_exclusion_adult,
                ceiling_exclusion_child=parent_item.ceiling_exclusion_child,
            )
        child.services.all().delete()
        for parent_svc in instance.services.all():
            ProductService.objects.create(
                audit_user_id=parent_svc.audit_user_id,
                product=child,
                service=parent_svc.service,
                price_origin=parent_svc.price_origin,
                limit_adult=parent_svc.limit_adult,
                limit_child=parent_svc.limit_child,
                waiting_period_adult=parent_svc.waiting_period_adult,
                waiting_period_child=parent_svc.waiting_period_child,
                limit_no_adult=parent_svc.limit_no_adult,
                limit_no_child=parent_svc.limit_no_child,
                limitation_type=parent_svc.limitation_type,
                limitation_type_r=parent_svc.limitation_type_r,
                limitation_type_e=parent_svc.limitation_type_e,
                limit_adult_r=parent_svc.limit_adult_r,
                limit_adult_e=parent_svc.limit_adult_e,
                limit_child_r=parent_svc.limit_child_r,
                limit_child_e=parent_svc.limit_child_e,
                ceiling_exclusion_adult=parent_svc.ceiling_exclusion_adult,
                ceiling_exclusion_child=parent_svc.ceiling_exclusion_child,
            )
