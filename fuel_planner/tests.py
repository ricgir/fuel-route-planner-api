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
        # Refills to 40 gallons (buys 30 gallons @ $2.00 = $60).
        # Drives final 300 miles to Finish, burning 30 gallons.
        # Total money spent at cash registers: $60.00
        self.assertAlmostEqual(res["total_cost"], 60.0, places=1)

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
        self.assertIn("no valid fueling plan", res["error"])

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
        self.assertIn("positive numbers", response2.json()["error"])

        # Invalid initial fuel
        response3 = self.client.get(reverse('route_api'), {
            'start': 'Chicago, IL',
            'finish': 'Houston, TX',
            'initial_fuel': '-10'
        })
        self.assertEqual(response3.status_code, 400)
        self.assertIn("cannot be negative", response3.json()["error"])
