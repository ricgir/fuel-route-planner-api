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

def find_optimal_fuel_stops(route_coords, stations, vehicle_range=500.0, mpg=10.0, initial_fuel=None, reserve_fuel=3.0):
    """
    Finds the mathematically optimal sequence of fuel stops using Dynamic Programming.
    
    Parameters:
      - route_coords: List of (lat, lon) along the route.
      - stations: QuerySet or list of FuelStation model instances.
      - vehicle_range: Max range of the vehicle in miles (default: 500).
      - mpg: Fuel efficiency in miles per gallon (default: 10).
      - initial_fuel: Initial fuel in gallons. If None, assumes full tank.
      - reserve_fuel: Safety fuel reserve in gallons (default: 3.0).
    
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

    if initial_fuel < reserve_fuel:
        return {
            "success": False,
            "error": f"Initial fuel ({initial_fuel:.2f} gal) is less than the required safety reserve ({reserve_fuel:.2f} gal)."
        }

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
    route_lats = np.array([c[0] for c in simplified_coords])
    route_lons = np.array([c[1] for c in simplified_coords])
    
    lat_min, lat_max = route_lats.min() - 0.2, route_lats.max() + 0.2
    lon_min, lon_max = route_lons.min() - 0.2, route_lons.max() + 0.2
    
    candidate_stations = []
    for s in stations:
        if lat_min <= s.latitude <= lat_max and lon_min <= s.longitude <= lon_max:
            candidate_stations.append(s)
            
    if not candidate_stations:
        # If no stations inside box (e.g. very short trip), check if start -> finish is possible
        if total_distance <= (initial_fuel - reserve_fuel) * mpg:
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
            return {"success": False, "error": f"Route is {total_distance:.1f} miles but initial safe range is only {(initial_fuel - reserve_fuel) * mpg:.1f} miles and no fueling stations are nearby."}

    # Compute distances from candidate stations to all route points
    station_lats = np.array([s.latitude for s in candidate_stations])
    station_lons = np.array([s.longitude for s in candidate_stations])
    
    dist_matrix = haversine_matrix(station_lats, station_lons, route_lats, route_lons)
    closest_route_indices = np.argmin(dist_matrix, axis=1)
    min_distances = dist_matrix[np.arange(len(candidate_stations)), closest_route_indices]
    
    # Filter stations within 10 miles of the route path corridor
    valid_indices = np.where(min_distances <= 10.0)[0]
    
    raw_stations = []
    for idx in valid_indices:
        station = candidate_stations[idx]
        route_point_idx = closest_route_indices[idx]
        raw_stations.append({
            "model": station,
            "route_dist": cumulative_dist[route_point_idx],
            "perp_dist": min_distances[idx]
        })
        
    # Sort stations along the route by cumulative route distance
    raw_stations.sort(key=lambda x: x["route_dist"])
    
    # Merge stations within 3.0 miles of each other, keeping only the cheapest one
    merged_stations = []
    if raw_stations:
        current_cluster = [raw_stations[0]]
        for item in raw_stations[1:]:
            if item["route_dist"] - current_cluster[0]["route_dist"] <= 3.0:
                current_cluster.append(item)
            else:
                # Keep the cheapest one in the cluster
                cheapest = min(current_cluster, key=lambda x: x["model"].retail_price)
                merged_stations.append(cheapest)
                current_cluster = [item]
        cheapest = min(current_cluster, key=lambda x: x["model"].retail_price)
        merged_stations.append(cheapest)

    # 3. DP Graph Construction
    M = len(merged_stations)
    nodes = []
    nodes.append({
        "type": "start",
        "dist": 0.0,
        "perp": 0.0,
        "price": 0.0,
        "station": None
    })
    for item in merged_stations:
        nodes.append({
            "type": "station",
            "dist": item["route_dist"],
            "perp": item["perp_dist"],
            "price": item["model"].retail_price,
            "station": item["model"]
        })
    nodes.append({
        "type": "finish",
        "dist": total_distance,
        "perp": 0.0,
        "price": 0.0,
        "station": None
    })
    
    def get_leg_distance(a, b):
        dist_a = nodes[a]["dist"]
        dist_b = nodes[b]["dist"]
        perp_a = nodes[a]["perp"]
        perp_b = nodes[b]["perp"]
        if a == 0:
            if b == M + 1:
                return dist_b
            return dist_b + perp_b
        else:
            if b == M + 1:
                return (dist_b - dist_a) + perp_a
            return (dist_b - dist_a) + perp_a + perp_b

    # dp[j][i]: min cost to reach node j having performed the last refuel at node i
    dp = [[float('inf')] * (M + 2) for _ in range(M + 2)]
    parent = [[-1] * (M + 2) for _ in range(M + 2)]
    
    # Base cases: Reach station j directly from Start (0)
    for j in range(1, M + 1):
        d_leg = get_leg_distance(0, j)
        if d_leg <= (initial_fuel - reserve_fuel) * mpg:
            dp[j][0] = 0.0
            parent[j][0] = -1

    # DP transitions: From last stop j, to next node k, coming from previous stop i
    for j in range(1, M + 1):
        for k in range(j + 1, M + 2):
            d_jk = get_leg_distance(j, k)
            if d_jk > (tank_capacity - reserve_fuel) * mpg:
                continue
                
            # Target fuel leaving j
            if k == M + 1:
                f_leave = d_jk / mpg + reserve_fuel
            else:
                if nodes[j]["price"] < nodes[k]["price"]:
                    f_leave = tank_capacity
                else:
                    f_leave = d_jk / mpg + reserve_fuel
                    
            for i in range(j):
                if dp[j][i] == float('inf'):
                    continue
                    
                # Fuel on arrival at j from path i -> j
                d_ij = get_leg_distance(i, j)
                if i == 0:
                    f_arr = initial_fuel - d_ij / mpg
                else:
                    if nodes[i]["price"] < nodes[j]["price"]:
                        f_arr = tank_capacity - d_ij / mpg
                    else:
                        f_arr = reserve_fuel
                        
                if f_arr < reserve_fuel:
                    continue
                    
                bought = max(0.0, f_leave - f_arr)
                cost_added = bought * nodes[j]["price"]
                if 0.0 < bought < 5.0:
                    cost_added += 1000.0  # Soft penalty for refueling less than 5 gallons
                total_cost = dp[j][i] + cost_added
                if total_cost < dp[k][j]:
                    dp[k][j] = total_cost
                    parent[k][j] = i

    # Find overall best path to Finish (M + 1)
    min_finish_cost = float('inf')
    best_last_stop = -1
    
    # Case A: Start -> Finish directly
    d_direct = get_leg_distance(0, M + 1)
    if d_direct <= (initial_fuel - reserve_fuel) * mpg:
        min_finish_cost = 0.0
        best_last_stop = 0
        
    # Case B: Some last stop j -> Finish
    for j in range(1, M + 1):
        for i in range(j):
            if dp[M + 1][j] < min_finish_cost:
                min_finish_cost = dp[M + 1][j]
                best_last_stop = j

    if min_finish_cost == float('inf'):
        return {"success": False, "error": f"Route is {total_distance:.1f} miles, but no valid fueling plan could be constructed with a maximum range of {vehicle_range:.1f} miles and the current station database."}

    # 4. Backtrack to reconstruct state path
    stops = []
    if best_last_stop > 0:
        best_state = (M + 1, best_last_stop)
        state_path = []
        curr_state = best_state
        while curr_state[1] > 0:
            state_path.append(curr_state)
            k, j = curr_state
            i = parent[k][j]
            curr_state = (j, i)
        state_path.reverse()
        
        # Re-simulate path for itinerary
        stop_details = []
        itinerary = []
        
        itinerary.append({
            "type": "start",
            "name": "Start Location",
            "distance": 0.0,
            "fuel_level": initial_fuel,
            "message": f"Start journey with {initial_fuel:.2f} gallons in the tank."
        })
        
        curr_fuel = initial_fuel
        curr_node = 0
        total_dist_traveled = 0.0
        
        for idx_path, (next_next_node, stop_node) in enumerate(state_path):
            station = nodes[stop_node]["station"]
            d_leg = get_leg_distance(curr_node, stop_node)
            total_dist_traveled += d_leg
            
            f_arr = curr_fuel - d_leg / mpg
            
            # Determine target fuel leaving stop_node
            d_next = get_leg_distance(stop_node, next_next_node)
            if next_next_node == M + 1:
                f_leave = d_next / mpg + reserve_fuel
            else:
                if nodes[stop_node]["price"] < nodes[next_next_node]["price"]:
                    f_leave = tank_capacity
                else:
                    f_leave = d_next / mpg + reserve_fuel
                    
            bought = max(0.0, f_leave - f_arr)
            cost = bought * nodes[stop_node]["price"]
            
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
                "distance_along_route": nodes[stop_node]["dist"],
                "detour_distance": nodes[stop_node]["perp"] * 2.0, # Round trip detour
                "fuel_bought": bought,
                "cost": cost
            })
            
            detour_str = f" (Detour: {nodes[stop_node]['perp'] * 2.0:.1f} mi)" if nodes[stop_node]['perp'] > 0 else ""
            itinerary.append({
                "type": "fuel_stop",
                "name": station.name,
                "city": station.city,
                "state": station.state,
                "distance": nodes[stop_node]["dist"],
                "leg_distance": d_leg,
                "fuel_before": f_arr,
                "fuel_bought": bought,
                "price": station.retail_price,
                "cost": cost,
                "message": f"Drive {d_leg:.1f} miles{detour_str}. Arrive at {station.name} ({station.city}, {station.state}) with {f_arr:.2f} gallons. Refill {bought:.2f} gallons at ${station.retail_price:.3f}/gal. Cost: ${cost:.2f}."
            })
            
            curr_fuel = f_leave
            curr_node = stop_node
            
        # Last leg to finish
        d_last = get_leg_distance(curr_node, M + 1)
        total_dist_traveled += d_last
        final_fuel = curr_fuel - d_last / mpg
        
        itinerary.append({
            "type": "finish",
            "name": "Destination",
            "distance": total_dist_traveled,
            "leg_distance": d_last,
            "fuel_level": final_fuel,
            "message": f"Drive final {d_last:.1f} miles. Arrive at destination with {final_fuel:.2f} gallons remaining."
        })
        
        return {
            "success": True,
            "stops": stop_details,
            "total_cost": min_finish_cost,
            "total_fuel_consumed": total_dist_traveled / mpg,
            "total_distance": total_dist_traveled,
            "itinerary": itinerary
        }
    else:
        # Start -> Finish directly
        d_direct = get_leg_distance(0, M + 1)
        return {
            "success": True,
            "stops": [],
            "total_cost": 0.0,
            "total_fuel_consumed": d_direct / mpg,
            "total_distance": d_direct,
            "itinerary": [
                {
                    "type": "start",
                    "name": "Start Location",
                    "distance": 0.0,
                    "fuel_level": initial_fuel,
                    "message": f"Start journey with {initial_fuel:.2f} gallons in the tank."
                },
                {
                    "type": "finish",
                    "name": "Destination",
                    "distance": d_direct,
                    "leg_distance": d_direct,
                    "fuel_level": initial_fuel - d_direct / mpg,
                    "message": f"Drive final {d_direct:.1f} miles. Arrive at destination with {initial_fuel - d_direct / mpg:.2f} gallons remaining."
                }
            ]
        }
