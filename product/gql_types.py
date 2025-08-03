import graphene
from graphene_django import DjangoObjectType
from .models import MembershipType
from location.models import Location


class RegionGQLType(DjangoObjectType):
    type = graphene.String()
    parent = graphene.Field(lambda: RegionGQLType)
    
    class Meta:
        model = Location
        fields = ("id", "uuid", "code", "name")
        
    def resolve_type(self, info):
        return self.type
        
    def resolve_parent(self, info):
        # Handle null parent_id safely
        if hasattr(self, 'parent_id') and self.parent_id:
            return Location.objects.filter(id=self.parent_id).first()
        return None


class DistrictGQLType(DjangoObjectType):
    parent = graphene.Field(lambda: RegionGQLType)
    type = graphene.String()
    
    class Meta:
        model = Location
        fields = ("id", "uuid", "code", "name")
        
    def resolve_parent(self, info):
        # Handle null parent_id safely
        if hasattr(self, 'parent_id') and self.parent_id:
            return Location.objects.filter(id=self.parent_id).first()
        return None
        
    def resolve_type(self, info):
        return self.type


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
            return Location.objects.filter(id=self.region_id).first()
        return None

    def resolve_district(self, info):
        if self.district_id:
            return Location.objects.filter(id=self.district_id).first()
        return None
