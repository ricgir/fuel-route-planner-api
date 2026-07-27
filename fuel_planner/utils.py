import urllib.request
import urllib.parse
import json
import numpy as np
from django.core.cache import cache

# Canadian provinces to filter out (routes are U.S.-only)
CANADIAN_PROVINCES = {'AB', 'BC', 'MB', 'NB', 'NS', 'ON', 'QC', 'SK', 'YT'}

def geocode_location(location_str):
    """
    Geocodes a location query string using Nominatim API.
    Uses Django cache to prevent redundant API calls.
    """
    cache_key = f"geocode_{urllib.parse.quote(location_str.lower().strip())}"
    cached_result = cache.get(cache_key)
    if cached_result:
        return cached_result

    url = f"https://nominatim.openstreetmap.org/search?q={urllib.parse.quote(location_str)}&format=json&limit=1"
    req = urllib.request.Request(
        url,
        headers={'User-Agent': 'FuelRoutePlanner/1.0 (contact@fuelplanner.com)'}
    )
    try:
        with urllib.request.urlopen(req) as response:
            data = json.loads(response.read().decode())
            if data:
                result = {
                    "lat": float(data[0]['lat']),
                    "lon": float(data[0]['lon']),
                    "name": data[0]['display_name']
                }
                cache.set(cache_key, result, timeout=86400) # Cache for 24 hours
                return result
    except Exception as e:
        print(f"Geocoding error for '{location_str}': {e}")
    return None

def fetch_route(start_coords, finish_coords):
    """
    Fetches driving route from OSRM API.
    Returns the route geometry (geojson) and distance in miles.
    """
    start_lat, start_lon = start_coords
    finish_lat, finish_lon = finish_coords
    
    cache_key = f"route_{start_lat}_{start_lon}_{finish_lat}_{finish_lon}"
    cached_result = cache.get(cache_key)
    if cached_result:
        return cached_result

    url = f"http://router.project-osrm.org/route/v1/driving/{start_lon},{start_lat};{finish_lon},{finish_lat}?overview=full&geometries=geojson"
    req = urllib.request.Request(
        url,
        headers={'User-Agent': 'FuelRoutePlanner/1.0 (contact@fuelplanner.com)'}
    )
    try:
        with urllib.request.urlopen(req) as response:
            data = json.loads(response.read().decode())
            if data and data.get("code") == "Ok" and "routes" in data:
                route = data["routes"][0]
                distance_miles = route["distance"] / 1609.344  # Convert meters to miles
                geometry = route["geometry"]
                result = {
                    "distance": distance_miles,
                    "geometry": geometry,
                    "duration": route["duration"] / 3600.0  # Duration in hours
                }
                cache.set(cache_key, result, timeout=86400) # Cache for 24 hours
                return result
    except Exception as e:
        print(f"Route fetching error: {e}")
    return None

def haversine_distance(lat1, lon1, lat2, lon2):
    """
    Computes haversine distance in miles between two coordinates.
    """
    R = 3958.8  # Earth radius in miles
    phi1, phi2 = np.radians(lat1), np.radians(lat2)
    dphi = np.radians(lat2 - lat1)
    dlambda = np.radians(lon2 - lon1)
    
    a = np.sin(dphi/2.0)**2 + np.cos(phi1) * np.cos(phi2) * np.sin(dlambda/2.0)**2
    c = 2.0 * np.arcsin(np.sqrt(a))
    return R * c

def haversine_matrix(lats1, lons1, lats2, lons2):
    """
    Computes pairwise haversine distance matrix between list 1 and list 2.
    lats1, lons1: shapes (M,)
    lats2, lons2: shapes (N,)
    returns: array of shape (M, N)
    """
    R = 3958.8
    lat1 = np.radians(lats1)[:, np.newaxis]
    lon1 = np.radians(lons1)[:, np.newaxis]
    lat2 = np.radians(lats2)[np.newaxis, :]
    lon2 = np.radians(lons2)[np.newaxis, :]
    
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    
    a = np.sin(dlat/2.0)**2 + np.cos(lat1) * np.cos(lat2) * np.sin(dlon/2.0)**2
    c = 2.0 * np.arcsin(np.sqrt(a))
    return R * c

def find_optimal_fuel_stops(route_coords, stations, vehicle_range=500.0, mpg=10.0, initial_fuel=None):
    """
    Finds the mathematically optimal sequence of fuel stops using Dynamic Programming.
    
    Parameters:
      - route_coords: List of (lat, lon) along the route.
      - stations: QuerySet or list of FuelStation model instances.
      - vehicle_range: Max range of the vehicle in miles (default: 500).
      - mpg: Fuel efficiency in miles per gallon (default: 10).
      - initial_fuel: Initial fuel in gallons. If None, assumes full tank (vehicle_range / mpg).
    
    Returns:
      A dictionary containing:
        - "success": Boolean indicating if a valid path was found.
        - "stops": List of dictionaries containing details of stops.
        - "total_cost": Total cost of fuel purchased at the registers.
        - "total_fuel_consumed": Total gallons burned.
        - "itinerary": Detailed text/data timeline of the trip.
    """
    if not route_coords:
        return {"success": False, "error": "Empty route geometry"}
        
    tank_capacity = vehicle_range / mpg
    if initial_fuel is None:
        initial_fuel = tank_capacity
    else:
        initial_fuel = min(float(initial_fuel), tank_capacity)

    # 1. Downsample route coords for performance (e.g. at most 300 points)
    n_coords = len(route_coords)
    step = max(1, n_coords // 300)
    simplified_coords = route_coords[::step]
    if simplified_coords[-1] != route_coords[-1]:
        simplified_coords.append(route_coords[-1])
        
    # Calculate cumulative distance along simplified route
    cumulative_dist = [0.0]
    for i in range(1, len(simplified_coords)):
        prev = simplified_coords[i-1]
        curr = simplified_coords[i]
        d = haversine_distance(prev[0], prev[1], curr[0], curr[1])
        cumulative_dist.append(cumulative_dist[-1] + d)
        
    total_distance = cumulative_dist[-1]
    
    # 2. Filter stations along the route using a spatial index (numpy broadcast)
    # Bounding box filter first
    route_lats = np.array([c[0] for c in simplified_coords])
    route_lons = np.array([c[1] for c in simplified_coords])
    
    lat_min, lat_max = route_lats.min() - 0.2, route_lats.max() + 0.2
    lon_min, lon_max = route_lons.min() - 0.2, route_lons.max() + 0.2
    
    # Filter stations in DB using bounding box
    candidate_stations = []
    for s in stations:
        if lat_min <= s.latitude <= lat_max and lon_min <= s.longitude <= lon_max:
            candidate_stations.append(s)
            
    if not candidate_stations:
        # If no stations inside box (e.g. very short trip), check if start -> finish is possible
        if total_distance <= initial_fuel * mpg:
            return {
                "success": True,
                "stops": [],
                "total_cost": 0.0,
                "total_fuel_consumed": total_distance / mpg,
                "total_distance": total_distance,
                "itinerary": [
                    {
                        "type": "start",
                        "distance": 0.0,
                        "fuel_level": initial_fuel,
                        "message": f"Start journey with {initial_fuel:.2f} gallons."
                    },
                    {
                        "type": "finish",
                        "distance": total_distance,
                        "fuel_level": initial_fuel - (total_distance / mpg),
                        "message": f"Arrive at destination. Remaining fuel: {initial_fuel - (total_distance / mpg):.2f} gallons."
                    }
                ]
            }
        else:
            return {"success": False, "error": f"Route is {total_distance:.1f} miles but initial range is only {initial_fuel * mpg:.1f} miles and no fueling stations are nearby."}

    # Compute distances from candidate stations to all route points
    station_lats = np.array([s.latitude for s in candidate_stations])
    station_lons = np.array([s.longitude for s in candidate_stations])
    
    # Pairwise distance matrix of shape (num_stations, num_route_points)
    dist_matrix = haversine_matrix(station_lats, station_lons, route_lats, route_lons)
    
    # Find closest route point for each station
    closest_route_indices = np.argmin(dist_matrix, axis=1)
    min_distances = dist_matrix[np.arange(len(candidate_stations)), closest_route_indices]
    
    # Filter stations within 10 miles of the route path
    valid_indices = np.where(min_distances <= 10.0)[0]
    
    stations_along_route = []
    for idx in valid_indices:
        station = candidate_stations[idx]
        route_point_idx = closest_route_indices[idx]
        stations_along_route.append({
            "model": station,
            "route_dist": cumulative_dist[route_point_idx],
            "perp_dist": min_distances[idx]
        })
        
    # Sort stations along the route by cumulative route distance
    stations_along_route.sort(key=lambda x: x["route_dist"])
    
    # 3. Dynamic Programming Formulation
    # Let 0 be Start (D=0), 1..M be stations, M+1 be Finish (D=total_distance)
    M = len(stations_along_route)
    nodes = []
    nodes.append({
        "type": "start",
        "dist": 0.0,
        "price": 0.0, # Start has no price
        "station": None
    })
    for item in stations_along_route:
        nodes.append({
            "type": "station",
            "dist": item["route_dist"],
            "price": item["model"].retail_price,
            "station": item["model"]
        })
    nodes.append({
        "type": "finish",
        "dist": total_distance,
        "price": 0.0,
        "station": None
    })
    
    # DP arrays
    # dp[j] is the min cost to reach node j with a full tank of fuel
    # parent[j] is the previous node index in the optimal path
    # fuel_on_arrival[j] is the fuel left when arriving at node j BEFORE refilling
    dp = [float('inf')] * (M + 2)
    parent = [-1] * (M + 2)
    fuel_on_arrival = [0.0] * (M + 2)
    
    # Base cases for nodes reachable directly from Start (0)
    for j in range(1, M + 1):
        d_j = nodes[j]["dist"]
        # Can we reach station j from start?
        if d_j <= initial_fuel * mpg:
            # Arrive at j with: initial_fuel - d_j / mpg
            rem_fuel = initial_fuel - d_j / mpg
            fuel_on_arrival[j] = rem_fuel
            # Cost to refill to capacity C at station j:
            dp[j] = (tank_capacity - rem_fuel) * nodes[j]["price"]
            parent[j] = 0

    # DP transitions between stations
    for j in range(1, M + 1):
        for i in range(1, j):
            if dp[i] == float('inf'):
                continue
            d_diff = nodes[j]["dist"] - nodes[i]["dist"]
            # Can we reach station j from station i?
            if d_diff <= vehicle_range:
                # We left i with a full tank (capacity). Arrive at j with:
                rem_fuel = tank_capacity - d_diff / mpg
                # Cost is: dp[i] + fuel we need to buy at j to get back to full tank
                cost = dp[i] + (tank_capacity - rem_fuel) * nodes[j]["price"]
                if cost < dp[j]:
                    dp[j] = cost
                    parent[j] = i
                    fuel_on_arrival[j] = rem_fuel

    # Transition to Finish (M + 1)
    min_finish_cost = float('inf')
    best_prev_node = -1
    
    # Case A: Reach Finish directly from Start (no stops)
    if total_distance <= initial_fuel * mpg:
        min_finish_cost = 0.0
        best_prev_node = 0
        fuel_on_arrival[M + 1] = initial_fuel - total_distance / mpg
        
    # Case B: Reach Finish from one of the stations
    for i in range(1, M + 1):
        if dp[i] == float('inf'):
            continue
        d_diff = total_distance - nodes[i]["dist"]
        if d_diff <= vehicle_range:
            # We left station i with a full tank.
            # We don't refill at the Finish, but the fuel we burned was purchased at station i.
            # So the cost is: dp[i] (which includes all prior refills)
            cost = dp[i]
            if cost < min_finish_cost:
                min_finish_cost = cost
                best_prev_node = i
                fuel_on_arrival[M + 1] = tank_capacity - d_diff / mpg

    if min_finish_cost == float('inf'):
        return {"success": False, "error": f"Route is {total_distance:.1f} miles, but no valid fueling plan could be constructed with a maximum range of {vehicle_range:.1f} miles and the current station database."}

    # Backtrack to reconstruct the optimal path of stops
    stops = []
    curr = best_prev_node
    while curr > 0:
        stops.append(curr)
        curr = parent[curr]
    stops.reverse()
    
    # Build detailed itinerary and stops list
    stop_details = []
    itinerary = []
    
    # Add start
    itinerary.append({
        "type": "start",
        "name": "Start Location",
        "distance": 0.0,
        "fuel_level": initial_fuel,
        "message": f"Start journey with {initial_fuel:.2f} gallons in the tank."
    })
    
    curr_fuel = initial_fuel
    prev_dist = 0.0
    
    for idx in stops:
        node = nodes[idx]
        station = node["station"]
        dist_to_stop = node["dist"]
        leg_dist = dist_to_stop - prev_dist
        
        # Fuel before refilling
        fuel_before = curr_fuel - (leg_dist / mpg)
        fuel_bought = tank_capacity - fuel_before
        cost = fuel_bought * station.retail_price
        
        stop_details.append({
            "id": station.id,
            "opis_id": station.opis_id,
            "name": station.name,
            "address": station.address,
            "city": station.city,
            "state": station.state,
            "price": station.retail_price,
            "latitude": station.latitude,
            "longitude": station.longitude,
            "distance_along_route": dist_to_stop,
            "fuel_bought": fuel_bought,
            "cost": cost
        })
        
        itinerary.append({
            "type": "fuel_stop",
            "name": station.name,
            "city": station.city,
            "state": station.state,
            "distance": dist_to_stop,
            "leg_distance": leg_dist,
            "fuel_before": fuel_before,
            "fuel_bought": fuel_bought,
            "price": station.retail_price,
            "cost": cost,
            "message": f"Drive {leg_dist:.1f} miles. Arrive at {station.name} ({station.city}, {station.state}) with {fuel_before:.2f} gallons. Refill {fuel_bought:.2f} gallons to full capacity ({tank_capacity:.1f} gallons) at ${station.retail_price:.3f}/gal. Cost: ${cost:.2f}."
        })
        
        # After stop, tank is full
        curr_fuel = tank_capacity
        prev_dist = dist_to_stop
        
    # Last leg to finish
    last_leg_dist = total_distance - prev_dist
    final_fuel = curr_fuel - (last_leg_dist / mpg)
    
    itinerary.append({
        "type": "finish",
        "name": "Destination",
        "distance": total_distance,
        "leg_distance": last_leg_dist,
        "fuel_level": final_fuel,
        "message": f"Drive final {last_leg_dist:.1f} miles. Arrive at destination with {final_fuel:.2f} gallons remaining."
    })
    
    return {
        "success": True,
        "stops": stop_details,
        "total_cost": min_finish_cost,
        "total_fuel_consumed": total_distance / mpg,
        "total_distance": total_distance,
        "itinerary": itinerary
    }
