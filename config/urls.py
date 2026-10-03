from django.urls import path

from flood.api import api
from flood.views import index

urlpatterns = [
    path("", index, name="index"),
    path("api/", api.urls),
]
