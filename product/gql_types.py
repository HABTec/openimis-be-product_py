import graphene
from graphene_django import DjangoObjectType
from .models import MembershipType
from location.models import Location


class RegionGQLType(DjangoObjectType):
    type = graphene.String()
    parent = graphene.Field(lambda: RegionGQLType)
    # Expose children as a Relay connection so clients can query edges/pageInfo
    children = graphene.relay.ConnectionField(lambda: DistrictGQLType._meta.connection)
    
    class Meta:
        model = Location
        fields = ("id", "uuid", "code", "name")
        interfaces = (graphene.relay.Node,)
        
    def resolve_children(self, info, **kwargs):
        # Return immediate children of this location
        return Location.objects.filter(parent_id=self.id)

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
    # Allow querying children with edges/node shape
    children = graphene.relay.ConnectionField(lambda: DistrictGQLType._meta.connection)
    
    class Meta:
        model = Location
        fields = ("id", "uuid", "code", "name")
        interfaces = (graphene.relay.Node,)
        
    def resolve_children(self, info, **kwargs):
        return Location.objects.filter(parent_id=self.id)

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
        )

