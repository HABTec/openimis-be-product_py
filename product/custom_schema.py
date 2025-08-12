import graphene
from graphene_django import DjangoObjectType
from core.models import User, InteractiveUser, Role
from product.models import Product
from .gql_types import MembershipTypeGQLType

from django.core.exceptions import PermissionDenied
from product.apps import ProductConfig
from core.gql_queries import RoleGQLType
from django.apps import apps


class TinyUserGQLType(DjangoObjectType):
    class Meta:
        model = User
        fields = ("id", "username")





class CustomProductGQLType(DjangoObjectType):
    membership_types = graphene.List(MembershipTypeGQLType)

    class Meta:
        model = Product
        fields = (
            "id",
            "code",
            "name",
            "enrolment_period_start_date",
            "enrolment_period_end_date",
            "chf_id_format",
            "lump_sum",
            "premium_adult",
            "max_installments",
            "registration_fee",
            "age_maximal",
            "card_replacement_fee",
        )

    def resolve_membership_types(self, info):
        return self.membership_types.all()
from django.utils.translation import gettext as _


class ProductLanguageGQLType(graphene.ObjectType):
    code = graphene.String()
    name = graphene.String()


class ProductLocationTinyGQLType(graphene.ObjectType):
    name = graphene.String()
    parent = graphene.Field(lambda: ProductLocationTinyGQLType)


class ProductUserDistrictGQLType(graphene.ObjectType):
    location = graphene.Field(ProductLocationTinyGQLType)


class CustomIUserGQLType(DjangoObjectType):
    # Return full language object to allow sub-selection (e.g., language { name })
    language = graphene.Field(ProductLanguageGQLType)
    # Expose additional contact fields
    phone = graphene.String()
    email = graphene.String()

    class Meta:
        model = InteractiveUser
        fields = (
            "id",
            "last_name",
            "other_names",
            "health_facility_id",
            # Include contact fields in GraphQL selection
            "phone",
            "email",
        )

    products = graphene.List(CustomProductGQLType)
    rights = graphene.List(graphene.String)
    has_password = graphene.Boolean()
    region = graphene.String()
    # Expose roles directly under iUser
    roles = graphene.List(RoleGQLType, description="List of current roles assigned to this interactive user")
    # Expose assigned districts with their locations
    userdistrictSet = graphene.List(ProductUserDistrictGQLType)

    def resolve_language(self, info):
        if not self.language:
            return None
        return ProductLanguageGQLType(code=self.language.code, name=self.language.name)

    def resolve_products(self, info):
        # This placeholder logic returns all valid products.
        # You may need to adjust this to fetch products specifically associated with the user.
        return Product.objects.filter(validity_to__isnull=True)

    def resolve_rights(self, info):
        return self.rights_str

    def resolve_has_password(self, info):
        return bool(self.password)

    def resolve_region(self, info):
        if self.health_facility and self.health_facility.location:
            return self.health_facility.location.name
        return None

    def resolve_roles(self, info, **kwargs):
        from django.utils.translation import gettext as _
        if not info.context.user.is_authenticated:
            raise PermissionDenied(_("unauthorized"))
        # Return active roles linked via the M2M-through user_roles
        if getattr(self, "user_roles", None):
            return Role.objects \
                .filter(validity_to__isnull=True) \
                .filter(user_roles__user_id=self.id, user_roles__validity_to__isnull=True)
        return None

    def resolve_userdistrictSet(self, info, **kwargs):
        # Lazy import to avoid cross-app hard dependency
        UserDistrict = apps.get_model("location", "UserDistrict")
        try:
            UserDistrict = apps.get_model("location", "UserDistrict")
        except LookupError:
            return []
        qs = UserDistrict.objects.filter(user_id=self.id, validity_to__isnull=True).select_related("location", "location__parent")
        # Map to lightweight GQL types (prefixed to avoid name collisions)
        result = []
        for ud in qs:
            loc = ud.location
            if not loc:
                continue
            parent = None
            if loc.parent:
                parent = ProductLocationTinyGQLType(name=loc.parent.name, parent=None)
            result.append(ProductUserDistrictGQLType(location=ProductLocationTinyGQLType(name=loc.name, parent=parent)))
        return result


class UserProductsGQLType(DjangoObjectType):
    class Meta:
        model = User
        fields = ("id", "username")

    i_user = graphene.Field(CustomIUserGQLType)
    t_user = graphene.Field(TinyUserGQLType)

    def resolve_i_user(self, info):
        return self.i_user

    def resolve_t_user(self, info):
        return self.t_user


class Query(graphene.ObjectType):
    user_products = graphene.Field(UserProductsGQLType, username=graphene.String())

    def resolve_user_products(self, info, username, **kwargs):
        if not info.context.user.has_perms(ProductConfig.gql_query_products_perms):
            raise PermissionDenied(_("unauthorized"))
        return User.objects.get(username=username)
