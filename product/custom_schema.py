import graphene
from graphene_django import DjangoObjectType
from core.models import User, InteractiveUser
from product.models import Product
from .gql_types import MembershipTypeGQLType

from django.core.exceptions import PermissionDenied
from product.apps import ProductConfig


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


class CustomIUserGQLType(DjangoObjectType):
    language = graphene.String()

    class Meta:
        model = InteractiveUser
        fields = (
            "id",
            "last_name",
            "other_names",
            "health_facility_id",
        )

    products = graphene.List(CustomProductGQLType)
    rights = graphene.List(graphene.String)
    has_password = graphene.Boolean()
    region = graphene.String()

    def resolve_language(self, info):
        return self.language.code

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
