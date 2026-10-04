from django.urls import path

from flood.api import api
from flood.views import about, index

urlpatterns = [
    path("", index, name="index"),
    path("about/", about, name="about"),
    path("api/", api.urls),
]
