import graphene
from graphene_django import DjangoObjectType
from location.models import Location

class SafeLocationGQLType(DjangoObjectType):
    """
    A safe version of the Location GraphQL type that handles null values properly.
    This can be used as a drop-in replacement for the standard Location type.
    """
    type = graphene.String()
    parent = graphene.Field(lambda: SafeLocationGQLType)
    
    class Meta:
        model = Location
        fields = ("id", "uuid", "code", "name")
        
    def resolve_type(self, info):
        return getattr(self, 'type', None)
        
    def resolve_parent(self, info):
        if hasattr(self, 'parent_id') and self.parent_id:
            return Location.objects.filter(id=self.parent_id).first()
        return None

# This function can be used to safely resolve location fields
def safe_resolve_location(obj, info):
    """
    Safely resolve a location field, handling null values properly.
    
    Args:
        obj: The object containing the location_id field
        info: The GraphQL info object
        
    Returns:
        The location object or None if not found
    """
    if not obj:
        return None
        
    location_id = getattr(obj, 'location_id', None)
    if not location_id:
        return None
        
    try:
        return Location.objects.get(id=location_id)
    except Location.DoesNotExist:
        return None
