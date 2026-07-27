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
        reserve_fuel = float(request.GET.get('reserve', 3.0))
        if vehicle_range <= 0 or mpg <= 0 or reserve_fuel < 0:
            raise ValueError()
    except ValueError:
        return JsonResponse({
            "success": False,
            "error": "Parameters 'range', 'mpg', and 'reserve' must be valid numbers (range and mpg positive, reserve non-negative)."
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
        initial_fuel=initial_fuel,
        reserve_fuel=reserve_fuel
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

MAJOR_US_CITIES = [
    "New York, NY", "Los Angeles, CA", "Chicago, IL", "Houston, TX", "Phoenix, AZ",
    "Philadelphia, PA", "San Antonio, TX", "San Diego, CA", "Dallas, TX", "San Jose, CA",
    "Austin, TX", "Jacksonville, FL", "Fort Worth, TX", "Columbus, OH", "Indianapolis, IN",
    "Charlotte, NC", "San Francisco, CA", "Seattle, WA", "Denver, CO", "Washington, DC",
    "Boston, MA", "El Paso, TX", "Nashville, TN", "Detroit, MI", "Oklahoma City, OK",
    "Portland, OR", "Las Vegas, NV", "Memphis, TN", "Louisville, KY", "Baltimore, MD",
    "Milwaukee, WI", "Albuquerque, NM", "Tucson, AZ", "Fresno, CA", "Sacramento, CA",
    "Kansas City, MO", "Mesa, AZ", "Atlanta, GA", "Omaha, NE", "Colorado Springs, CO",
    "Raleigh, NC", "Virginia Beach, VA", "Long Beach, CA", "Miami, FL", "Oakland, CA",
    "Minneapolis, MN", "Tulsa, OK", "Bakersfield, CA", "Tampa, FL", "Wichita, KS",
    "Arlington, TX", "Aurora, CO", "New Orleans, LA", "Cleveland, OH", "Anaheim, CA",
    "Henderson, NV", "Honolulu, HI", "Santa Ana, CA", "Riverside, CA", "Corpus Christi, TX",
    "Lexington, KY", "San Juan, PR", "Stockton, CA", "St. Paul, MN", "Cincinnati, OH",
    "Irvine, CA", "Greensboro, NC", "Pittsburgh, PA", "Lincoln, NE", "Durham, NC",
    "Orlando, FL", "Laredo, TX", "Anchorage, AK", "Chula Vista, CA", "Plano, TX",
    "Newark, NJ", "Toledo, OH", "Fort Wayne, IN", "St. Petersburg, FL", "Lubbock, TX",
    "St. Louis, MO", "Reno, NV", "Buffalo, NY", "Scottsdale, AZ", "Madison, WI",
    "Chandler, AZ", "Chesapeake, VA", "Glendale, AZ", "Gilbert, AZ", "Winston-Salem, NC",
    "North Las Vegas, NV", "Irving, TX", "Fremont, CA", "Garland, TX", "Hialeah, FL",
    "Arlington, VA", "Richmond, VA", "Boise, ID", "Spokane, WA", "Baton Rouge, LA",
    "Tacoma, WA", "Des Moines, IA"
]

@require_GET
def cities_autocomplete_view(request):
    """
    Returns unique city/state combinations matching the query 'q'.
    Includes both popular US cities and cities from the fuel station database.
    """
    q = request.GET.get('q', '').strip()
    if not q or len(q) < 2:
        return JsonResponse({"cities": []})
        
    q_lower = q.lower()
    
    # 1. Search popular US cities
    popular_matches = []
    for city in MAJOR_US_CITIES:
        if q_lower in city.lower():
            popular_matches.append(city)
            
    # 2. Search fuel station cities in database
    db_matches = FuelStation.objects.filter(city__icontains=q).values('city', 'state').distinct()[:15]
    db_cities = [f"{m['city']}, {m['state']}" for m in db_matches]
    
    # Combine and deduplicate
    seen = set()
    combined = []
    for city in popular_matches + db_cities:
        city_norm = city.lower().strip()
        if city_norm not in seen:
            seen.add(city_norm)
            combined.append(city)
            
    # Sort: prioritize cities whose name starts with q, then the rest
    def sort_key(city_str):
        name_only = city_str.split(',')[0].lower().strip()
        starts = name_only.startswith(q_lower)
        return (not starts, city_str.lower())
        
    combined.sort(key=sort_key)
    
    return JsonResponse({"cities": combined[:10]})
