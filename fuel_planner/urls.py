from django.urls import path
from fuel_planner.views import home_view, route_api_view

urlpatterns = [
    path('', home_view, name='home'),
    path('api/route/', route_api_view, name='route_api'),
]
