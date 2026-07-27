import os
import csv
from django.core.management.base import BaseCommand
from fuel_planner.models import FuelStation

CANADIAN_PROVINCES = {'AB', 'BC', 'MB', 'NB', 'NS', 'ON', 'QC', 'SK', 'YT'}

# Hardcoded coordinates for the 6 missing US cities
MISSING_CITIES_COORDS = {
    ('brookpark', 'oh'): (41.4197915, -81.8238184),
    ('elizabethport', 'nj'): (40.6501031, -74.1870888),
    ('evergreen', 'al'): (31.4334994, -86.9569176),
    ('henrico', 'va'): (37.5131191, -77.3465081),
    ('port wentworth', 'ga'): (32.1490920, -81.1631681),
    ('university park', 'il'): (41.4400344, -87.6833770),
}

class Command(BaseCommand):
    help = 'Import fuel stations from CSV and geocode them offline'

    def handle(self, *args, **options):
        # Paths
        base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
        cities_path = os.path.join(base_dir, 'us_cities.csv')
        prices_path = os.path.join(base_dir, 'fuel-prices-for-be-assessment.csv')

        if not os.path.exists(cities_path):
            self.stdout.write(self.style.ERROR(f"Cities database not found at {cities_path}"))
            return

        if not os.path.exists(prices_path):
            self.stdout.write(self.style.ERROR(f"Fuel prices CSV not found at {prices_path}"))
            return

        self.stdout.write("Loading US cities database...")
        city_coords = {}
        with open(cities_path, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                state = row['STATE_CODE'].strip().lower()
                city = row['CITY'].strip().lower()
                lat = float(row['LATITUDE'])
                lng = float(row['LONGITUDE'])
                city_coords[(city, state)] = (lat, lng)

        # Clear existing fuel stations
        self.stdout.write("Clearing existing fuel stations...")
        FuelStation.objects.all().delete()

        self.stdout.write("Importing fuel stations...")
        stations_to_create = []
        skipped_canadian = 0
        skipped_no_coord = 0
        total_rows = 0

        with open(prices_path, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                total_rows += 1
                state = row['State'].strip().upper()
                if state in CANADIAN_PROVINCES:
                    skipped_canadian += 1
                    continue

                city = row['City'].strip().lower()
                state_lower = state.lower()

                # Get coordinates
                coords = None
                if (city, state_lower) in city_coords:
                    coords = city_coords[(city, state_lower)]
                elif (city, state_lower) in MISSING_CITIES_COORDS:
                    coords = MISSING_CITIES_COORDS[(city, state_lower)]

                if not coords:
                    skipped_no_coord += 1
                    # Log missing ones
                    self.stdout.write(self.style.WARNING(f"No coordinates found for {row['City']}, {row['State']}"))
                    continue

                try:
                    retail_price = float(row['Retail Price'])
                except ValueError:
                    retail_price = 0.0

                station = FuelStation(
                    opis_id=int(row['OPIS Truckstop ID']),
                    name=row['Truckstop Name'].strip(),
                    address=row['Address'].strip(),
                    city=row['City'].strip(),
                    state=state,
                    rack_id=int(row['Rack ID']),
                    retail_price=retail_price,
                    latitude=coords[0],
                    longitude=coords[1]
                )
                stations_to_create.append(station)

        # Bulk create for fast insertion
        self.stdout.write(f"Saving {len(stations_to_create)} fuel stations...")
        FuelStation.objects.bulk_create(stations_to_create)

        self.stdout.write(self.style.SUCCESS(
            f"Import complete!\n"
            f"Total rows processed: {total_rows}\n"
            f"Successfully imported: {len(stations_to_create)}\n"
            f"Skipped (Canadian): {skipped_canadian}\n"
            f"Skipped (No coords found): {skipped_no_coord}"
        ))
