from graphene_django import DjangoObjectType
from .models import MembershipType


class MembershipTypeGQLType(DjangoObjectType):
    class Meta:
        model = MembershipType
        fields = "__all__"
