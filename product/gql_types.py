import graphene
from graphene_django import DjangoObjectType
from .models import MembershipType
from location.models import Location


class RegionGQLType(DjangoObjectType):
    class Meta:
        model = Location
        fields = ("id", "uuid", "code", "name")


class DistrictGQLType(DjangoObjectType):
    class Meta:
        model = Location
        fields = ("id", "uuid", "code", "name")


class MembershipTypeGQLType(DjangoObjectType):
    class Meta:
        model = MembershipType
        fields = (
            "id",
            "level_type",
            "level_index",
            "price",
            "is_indigent",
            "region",
            "district",
        )

    region = graphene.Field(RegionGQLType)
    district = graphene.Field(DistrictGQLType)

    def resolve_region(self, info):
        if self.region_id:
            return Location.objects.get(id=self.region_id)
        return None

    def resolve_district(self, info):
        if self.district_id:
            return Location.objects.get(id=self.district_id)
        return None
