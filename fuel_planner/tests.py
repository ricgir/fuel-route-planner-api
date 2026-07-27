from django.test import TestCase, Client
from django.urls import reverse
from unittest.mock import patch
from fuel_planner.models import FuelStation
from fuel_planner.utils import (
    haversine_distance,
    geocode_location,
    fetch_route,
    find_optimal_fuel_stops
)

class FuelPlannerTests(TestCase):
    def setUp(self):
        self.client = Client()
        # Seed test stations
        self.station1 = FuelStation.objects.create(
            opis_id=101,
            name="Cheap Station A",
            address="123 Main St",
            city="CityA",
            state="IL",
            rack_id=999,
            retail_price=2.00,
            latitude=41.0,
            longitude=-87.0
        )
        self.station2 = FuelStation.objects.create(
            opis_id=102,
            name="Expensive Station B",
            address="456 Oak Rd",
            city="CityB",
            state="IN",
            rack_id=999,
            retail_price=4.00,
            latitude=41.1,
            longitude=-86.0
        )

    def test_haversine_distance(self):
        # Distance between Chicago (41.8781, -87.6298) and Milwaukee (43.0389, -87.9065)
        # matches around 83-90 miles
        dist = haversine_distance(41.8781, -87.6298, 43.0389, -87.9065)
        self.assertTrue(80 < dist < 95)

    @patch('urllib.request.urlopen')
    def test_geocode_location_success(self, mock_urlopen):
        # Mock response data for Nominatim
        mock_response = mock_urlopen.return_value.__enter__.return_value
        mock_response.read.return_value = b'[{"lat": "41.8781", "lon": "-87.6298", "display_name": "Chicago, IL, USA"}]'
        
        result = geocode_location("Chicago, IL")
        self.assertIsNotNone(result)
        self.assertEqual(result["lat"], 41.8781)
        self.assertEqual(result["lon"], -87.6298)
        self.assertEqual(result["name"], "Chicago, IL, USA")

    @patch('urllib.request.urlopen')
    def test_fetch_route_success(self, mock_urlopen):
        # Mock response data for OSRM
        mock_response = mock_urlopen.return_value.__enter__.return_value
        mock_response.read.return_value = (
            b'{"code": "Ok", "routes": [{"distance": 160934.4, "duration": 3600.0, '
            b'"geometry": {"coordinates": [[-87.6298, 41.8781], [-87.0, 41.0], [-86.0, 41.1]], "type": "LineString"}}]}'
        )
        
        result = fetch_route((41.8781, -87.6298), (41.1, -86.0))
        self.assertIsNotNone(result)
        # 160934.4 meters is 100 miles
        self.assertAlmostEqual(result["distance"], 100.0, places=2)
        self.assertEqual(result["geometry"]["type"], "LineString")

    def test_dp_optimizer_no_stops_needed(self):
        # Route points Chicago -> Indiana (100 miles)
        # Vehicle has 500-mile range and starts full (initial_fuel = 50 gal, 10 MPG = 500 miles range)
        route_coords = [(41.8781, -87.6298), (41.0, -87.0), (41.1, -86.0)]
        stations = FuelStation.objects.all()
        
        # 100 miles total, initial range is 500. No stops needed.
        res = find_optimal_fuel_stops(route_coords, stations, vehicle_range=500.0, mpg=10.0)
        self.assertTrue(res["success"])
        self.assertEqual(len(res["stops"]), 0)
        self.assertEqual(res["total_cost"], 0.0)
        self.assertAlmostEqual(res["total_fuel_consumed"], 12.14, places=1)

    def test_dp_optimizer_stops_required(self):
        # Route is 600 miles. Start (0, 0) -> Station1 (300, 0) -> Finish (600, 0)
        # Range is 400 miles. mpg is 10.
        # Cheap Station A at 300 miles.
        route_coords = [
            (0.0, 0.0),
            (2.0, 0.0), # roughly 140 miles
            (4.0, 0.0), # roughly 276 miles
            (4.34, 0.0), # approx 300 miles
            (8.68, 0.0) # approx 600 miles
        ]
        
        # Let's override station coordinates so they align with our route coordinates
        # Station A is at (4.34, 0.0), which is ~300 miles. Price = $2.00
        # Station B is at (6.0, 0.0), which is ~414 miles. Price = $4.00
        self.station1.latitude = 4.34
        self.station1.longitude = 0.0
        self.station1.save()
        
        self.station2.latitude = 6.0
        self.station2.longitude = 0.0
        self.station2.save()
        
        stations = FuelStation.objects.all()
        
        # 600 miles, range 400, starts full. Must stop at Station A (300 miles) or B (414 miles).
        # A is cheaper, so it should choose A.
        res = find_optimal_fuel_stops(route_coords, stations, vehicle_range=400.0, mpg=10.0)
        self.assertTrue(res["success"])
        self.assertEqual(len(res["stops"]), 1)
        self.assertEqual(res["stops"][0]["opis_id"], 101)  # Cheap Station A
        
        # Total cost: 600 miles total, burns 60 gallons.
        # Starts with 40 gallons. Arrives at Station A (300 miles) with 10 gallons.
        # Next leg is 300 miles. Leaving A requires 30 + 3 (reserve) = 33 gallons.
        # We buy 33 - 10 = 23 gallons @ $2.00 = $46.00.
        # (This is more cost-effective than refueling to capacity since Finish is cheaper/free).
        self.assertAlmostEqual(res["total_cost"], 46.0, delta=0.1)

    def test_dp_optimizer_initial_fuel_constraint(self):
        # Route points (0, 0) -> Station A (1.0, 0.0) (~69.1 miles) -> Finish (2.0, 0.0) (~138.2 miles)
        # Station A is at (1.0, 0.0)
        self.station1.latitude = 1.0
        self.station1.longitude = 0.0
        self.station1.save()
        
        stations = FuelStation.objects.all()
        
        # If we start with only 5 gallons (range 50 miles) and mpg 10
        # We can't even reach Station A at 69.1 miles.
        route_coords = [(0.0, 0.0), (1.0, 0.0), (2.0, 0.0)]
        res = find_optimal_fuel_stops(route_coords, stations, vehicle_range=500.0, mpg=10.0, initial_fuel=5.0)
        self.assertFalse(res["success"])
        self.assertIn("no valid fueling plan could be constructed", res["error"])

        # If initial fuel is less than the safety reserve itself (e.g. 2.0 gallons)
        res_reserve = find_optimal_fuel_stops(route_coords, stations, vehicle_range=500.0, mpg=10.0, initial_fuel=2.0)
        self.assertFalse(res_reserve["success"])
        self.assertIn("less than the required safety reserve", res_reserve["error"])

        # If we start with 10 gallons (range 100 miles), we can reach Station A
        # but we cannot reach the Finish (138.2 miles) directly. So we must stop at Station A.
        res2 = find_optimal_fuel_stops(route_coords, stations, vehicle_range=500.0, mpg=10.0, initial_fuel=10.0)
        self.assertTrue(res2["success"])
        self.assertEqual(len(res2["stops"]), 1)
        self.assertEqual(res2["stops"][0]["opis_id"], 101)

    def test_api_validation(self):
        # Missing start/finish
        response = self.client.get(reverse('route_api'), {'start': 'Chicago, IL'})
        self.assertEqual(response.status_code, 400)
        self.assertIn("required", response.json()["error"])
        
        # Invalid numeric parameters
        response2 = self.client.get(reverse('route_api'), {
            'start': 'Chicago, IL',
            'finish': 'Houston, TX',
            'range': '-500'
        })
        self.assertEqual(response2.status_code, 400)
        self.assertIn("valid numbers", response2.json()["error"])

        # Invalid initial fuel
        response3 = self.client.get(reverse('route_api'), {
            'start': 'Chicago, IL',
            'finish': 'Houston, TX',
            'initial_fuel': '-10'
        })
        self.assertEqual(response3.status_code, 400)
        self.assertIn("cannot be negative", response3.json()["error"])

    def test_avoid_small_refill_penalty(self):
        # Set up 4 stations:
        # A: ~50 miles. Price = $2.00
        self.station1.latitude = 50.0 / 69.11
        self.station1.longitude = 0.0
        self.station1.retail_price = 2.00
        self.station1.save()
        
        # B: ~100 miles. Price = $2.00
        self.station2.latitude = 100.0 / 69.11
        self.station2.longitude = 0.0
        self.station2.retail_price = 2.00
        self.station2.save()
        
        # C: ~150 miles. Price = $2.00
        station3 = FuelStation.objects.create(
            opis_id=103,
            name="Station C",
            address="789 Pine Rd",
            city="CityC",
            state="IN",
            rack_id=999,
            retail_price=2.00,
            latitude=150.0 / 69.11,
            longitude=0.0
        )
        
        # D: ~120 miles. Price = $2.00
        station4 = FuelStation.objects.create(
            opis_id=104,
            name="Station D",
            address="101 Maple Ave",
            city="CityD",
            state="IN",
            rack_id=999,
            retail_price=2.00,
            latitude=120.0 / 69.11,
            longitude=0.0
        )
        
        stations = FuelStation.objects.all()
        # Route from 0 to 200 miles
        route_coords = [
            (0.0, 0.0),
            (50.0 / 69.11, 0.0),
            (100.0 / 69.11, 0.0),
            (120.0 / 69.11, 0.0),
            (150.0 / 69.11, 0.0),
            (200.0 / 69.11, 0.0)
        ]
        
        # vehicle_range = 100, mpg = 10, initial_fuel = 10.0, reserve_fuel = 3.0
        # If we take path Start -> A -> D -> C -> Finish, we'd need a 1.0 gallon refill at C.
        # If we take path Start -> A -> B -> C -> Finish, we make 5.0 gallon refills everywhere.
        # The penalty-aware solver should choose [A, B, C] over [A, D, C].
        res = find_optimal_fuel_stops(route_coords, stations, vehicle_range=100.0, mpg=10.0, initial_fuel=10.0, reserve_fuel=3.0)
        self.assertTrue(res["success"])
        self.assertEqual(len(res["stops"]), 3)
        self.assertEqual(res["stops"][0]["opis_id"], 101) # A
        self.assertEqual(res["stops"][1]["opis_id"], 102) # B
        self.assertEqual(res["stops"][2]["opis_id"], 103) # C

    def test_zero_gallon_stops_prevented(self):
        # Route is 100 miles. Start (0,0) -> Station A (40 miles) -> Finish (100 miles).
        # Range = 500 miles, initial_fuel = 50.0 (full).
        self.station1.latitude = 40.0 / 69.11
        self.station1.longitude = 0.0
        self.station1.save()
        
        stations = FuelStation.objects.all()
        route_coords = [(0.0, 0.0), (40.0 / 69.11, 0.0), (100.0 / 69.11, 0.0)]
        res = find_optimal_fuel_stops(route_coords, stations, vehicle_range=500.0, mpg=10.0, initial_fuel=50.0, reserve_fuel=3.0)
        self.assertTrue(res["success"])
        # Should have 0 stops because initial fuel is more than enough, and stopping at A to buy 0 gallons is skipped.
        self.assertEqual(len(res["stops"]), 0)

    def test_fuel_reserve_limitations(self):
        # If safety reserve >= tank capacity, it is infeasible.
        route_coords = [(0.0, 0.0), (100.0 / 69.11, 0.0)]
        stations = FuelStation.objects.all()
        res = find_optimal_fuel_stops(route_coords, stations, vehicle_range=100.0, mpg=10.0, initial_fuel=10.0, reserve_fuel=10.0)
        self.assertFalse(res["success"])

    def test_detour_distance_calculations(self):
        # Station is off-route. Test that perp distance is calculated and included.
        # Route: Start (0,0) -> Finish (100 miles).
        # Station at 50 miles along, and 2 miles perpendicular detour.
        self.station1.latitude = 50.0 / 69.11
        self.station1.longitude = 2.0 / 52.0
        self.station1.save()
        
        stations = FuelStation.objects.all()
        route_coords = [(0.0, 0.0), (50.0 / 69.11, 0.0), (100.0 / 69.11, 0.0)]
        res = find_optimal_fuel_stops(route_coords, stations, vehicle_range=100.0, mpg=10.0, initial_fuel=7.0, reserve_fuel=1.0)
        self.assertTrue(res["success"])
        # Should stop at station1
        self.assertEqual(len(res["stops"]), 1)
        # Detour distance should be greater than 0.0
        self.assertTrue(res["stops"][0]["detour_distance"] > 0.0)

    def test_station_merging_cheapest(self):
        # Seed two stations very close to each other (within 3 miles):
        # Station A: $3.00, Station B: $2.00
        # Check that only the cheaper one is kept.
        self.station1.latitude = 50.0 / 69.11
        self.station1.longitude = 0.0
        self.station1.retail_price = 3.00
        self.station1.save()
        
        self.station2.latitude = 50.1 / 69.11
        self.station2.longitude = 0.0
        self.station2.retail_price = 2.00
        self.station2.save()
        
        stations = FuelStation.objects.all()
        route_coords = [(0.0, 0.0), (50.0 / 69.11, 0.0), (100.0 / 69.11, 0.0)]
        res = find_optimal_fuel_stops(route_coords, stations, vehicle_range=100.0, mpg=10.0, initial_fuel=7.0, reserve_fuel=1.0)
        self.assertTrue(res["success"])
        self.assertEqual(len(res["stops"]), 1)
        # Should pick the cheaper one (station2, price $2.00)
        self.assertEqual(res["stops"][0]["opis_id"], 102)

    def test_initial_fuel_default(self):
        # Test that without specifying initial fuel, it defaults to a full tank
        route_coords = [(0.0, 0.0), (100.0 / 69.11, 0.0)]
        stations = FuelStation.objects.all()
        res = find_optimal_fuel_stops(route_coords, stations, vehicle_range=500.0, mpg=10.0, reserve_fuel=3.0)
        self.assertTrue(res["success"])
        self.assertEqual(res["itinerary"][0]["fuel_level"], 50.0)

    def test_flexible_refuel_pricing(self):
        # Expensive station A ($4.00) followed by a cheap station B ($2.00).
        # We should only buy enough at A to safely reach B.
        self.station1.latitude = 50.0 / 69.11
        self.station1.longitude = 0.0
        self.station1.retail_price = 4.00
        self.station1.save()
        
        self.station2.latitude = 100.0 / 69.11
        self.station2.longitude = 0.0
        self.station2.retail_price = 2.00
        self.station2.save()
        
        stations = FuelStation.objects.all()
        route_coords = [(0.0, 0.0), (50.0 / 69.11, 0.0), (100.0 / 69.11, 0.0), (170.0 / 69.11, 0.0)]
        res = find_optimal_fuel_stops(route_coords, stations, vehicle_range=100.0, mpg=10.0, initial_fuel=7.0, reserve_fuel=2.0)
        self.assertTrue(res["success"])
        self.assertEqual(len(res["stops"]), 2)
        # First stop (A): should buy exactly 5.0 gallons (flexible refuel, not filling to capacity)
        self.assertAlmostEqual(res["stops"][0]["fuel_bought"], 5.0, places=2)
        # Second stop (B): should refuel to 9.0 gallons (buy 7.0 gallons > 5.0)
        self.assertTrue(res["stops"][1]["fuel_bought"] > 5.0)

    def test_minimum_refuel_threshold_avoidance(self):
        # Route is 120 miles. Start -> A (50 miles) -> Finish (120 miles).
        # Range = 500, mpg = 10. Initial fuel = 14.0.
        # We need to buy 1.0 gallon at A to reach finish safely, but it gets adjusted to 5.0 to avoid penalty.
        self.station1.latitude = 50.0 / 69.11
        self.station1.longitude = 0.0
        self.station1.save()
        
        stations = FuelStation.objects.all()
        route_coords = [(0.0, 0.0), (50.0 / 69.11, 0.0), (120.0 / 69.11, 0.0)]
        res = find_optimal_fuel_stops(route_coords, stations, vehicle_range=500.0, mpg=10.0, initial_fuel=14.0, reserve_fuel=3.0)
        self.assertTrue(res["success"])
        self.assertEqual(len(res["stops"]), 1)
        self.assertAlmostEqual(res["stops"][0]["fuel_bought"], 5.0, places=2)

    def test_cities_autocomplete(self):
        # Create a unique station in a test city
        FuelStation.objects.create(
            opis_id=105,
            name="Test Autocomplete Station",
            address="102 Elm Rd",
            city="Champaign",
            state="IL",
            rack_id=999,
            retail_price=2.50,
            latitude=40.11,
            longitude=-88.24
        )
        
        # Test query matching
        response = self.client.get(reverse('cities_autocomplete'), {'q': 'cham'})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("Champaign, IL", data["cities"])
        
        # Test query too short
        response_short = self.client.get(reverse('cities_autocomplete'), {'q': 'c'})
        self.assertEqual(response_short.status_code, 200)
        self.assertEqual(response_short.json()["cities"], [])
