from django.shortcuts import render
from django.http import JsonResponse
from django.views.decorators.http import require_GET
from fuel_planner.models import FuelStation
from fuel_planner.utils import geocode_location, fetch_route, find_optimal_fuel_stops

@require_GET
def home_view(request):
    """
    Renders the main interactive map dashboard.
    """
    return render(request, 'index.html')

@require_GET
def route_api_view(request):
    """
    API endpoint that plans the route and finds optimal fuel stops.
    Query parameters:
      - start: text description of start location (e.g. "Seattle, WA")
      - finish: text description of finish location (e.g. "Los Angeles, CA")
      - range: vehicle range in miles (default: 500)
      - mpg: miles per gallon (default: 10)
      - initial_fuel: initial gallons of gas (default: full tank)
    """
    start_str = request.GET.get('start', '').strip()
    finish_str = request.GET.get('finish', '').strip()
    
    if not start_str or not finish_str:
        return JsonResponse({
            "success": False,
            "error": "Both 'start' and 'finish' query parameters are required."
        }, status=400)
        
    try:
        vehicle_range = float(request.GET.get('range', 500.0))
        mpg = float(request.GET.get('mpg', 10.0))
        if vehicle_range <= 0 or mpg <= 0:
            raise ValueError()
    except ValueError:
        return JsonResponse({
            "success": False,
            "error": "Parameters 'range' and 'mpg' must be positive numbers."
        }, status=400)

    # Tank capacity
    tank_capacity = vehicle_range / mpg
    
    initial_fuel_str = request.GET.get('initial_fuel', '').strip()
    if initial_fuel_str:
        try:
            initial_fuel = float(initial_fuel_str)
            if initial_fuel < 0:
                return JsonResponse({
                    "success": False,
                    "error": "Parameter 'initial_fuel' cannot be negative."
                }, status=400)
            initial_fuel = min(initial_fuel, tank_capacity)
        except ValueError:
            return JsonResponse({
                "success": False,
                "error": "Parameter 'initial_fuel' must be a valid number."
            }, status=400)
    else:
        initial_fuel = tank_capacity

    # 1. Geocode locations
    start_loc = geocode_location(start_str)
    if not start_loc:
        return JsonResponse({
            "success": False,
            "error": f"Could not geocode start location '{start_str}'"
        }, status=404)
        
    finish_loc = geocode_location(finish_str)
    if not finish_loc:
        return JsonResponse({
            "success": False,
            "error": f"Could not geocode finish location '{finish_str}'"
        }, status=404)

    # 2. Get route from OSRM
    start_coords = (start_loc["lat"], start_loc["lon"])
    finish_coords = (finish_loc["lat"], finish_loc["lon"])
    
    route_data = fetch_route(start_coords, finish_coords)
    if not route_data:
        return JsonResponse({
            "success": False,
            "error": "Could not calculate driving route between locations."
        }, status=502)

    # 3. Retrieve all fuel stations
    stations = FuelStation.objects.all()

    # Extract route coordinates from geojson geometry (geometry['coordinates'] is [lon, lat])
    # Convert to list of (lat, lon)
    route_coords = [(c[1], c[0]) for c in route_data["geometry"]["coordinates"]]

    # 4. Find optimal stops
    optimization_result = find_optimal_fuel_stops(
        route_coords=route_coords,
        stations=stations,
        vehicle_range=vehicle_range,
        mpg=mpg,
        initial_fuel=initial_fuel
    )
    
    if not optimization_result["success"]:
        return JsonResponse(optimization_result, status=422)

    # Append geo-coordinates of start/finish for response helper
    response_data = {
        "success": True,
        "start": start_loc,
        "finish": finish_loc,
        "route_distance_miles": route_data["distance"],
        "route_duration_hours": route_data["duration"],
        "route_geometry": route_data["geometry"],
        "total_cost": optimization_result["total_cost"],
        "total_fuel_consumed_gallons": optimization_result["total_fuel_consumed"],
        "stops": optimization_result["stops"],
        "itinerary": optimization_result["itinerary"]
    }
    
    return JsonResponse(response_data)
