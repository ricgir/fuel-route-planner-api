# Fuel Stop Route Planner API & Interactive Dashboard

A high-performance, cost-optimized vehicle routing and refueling planner designed for long-distance travel in the USA.

The application calculates the most cost-effective fuel stops along a driving route between any two US locations, using a 2D Dynamic Programming (DP) algorithm and a preprocessed dataset of 7,531 fuel stations.

---

## 🚀 Key Engineering & Algorithmic Highlights

### ⚡ Smart API Caching & Efficiency
* **Geocoding & Route Cache**: To minimize network latency and prevent external API throttling, Nominatim geocoding and OSRM routing responses are cached locally (valid for 24 hours).
* **Single Request Constraint**: Exactly **one routing request** is made per unique origin–destination pair.
* **Pure Local Execution**: All downstream computations—including fuel stop filtering, spatial distance lookups, clustering, and the 2D DP optimization solver—run entirely locally on the SQLite database in under **10 milliseconds**.

### 🧠 2D Dynamic Programming Optimizer
Unlike simple greedy algorithms, the custom solver implements a state-space model tracking `(station, last_refuel_station)` to make globally optimal choices:
* **Flexible Purchase Quantity**: Refuels only as much as necessary to reach a cheaper station or the final destination, rather than naively refilling the tank to full capacity.
* **Safety Fuel Reserve**: Enforces a minimum safety reserve in the tank (e.g., 3.0 gallons) at any point along the journey.
* **Detour Distance Modeling**: Calculates exact perpendicular detour distances to and from stations near the corridor, incorporating them directly into the cost function.
* **Candidate Station Pruning & Merging**: Merges stations within 3.0 miles of each other, keeping only the cheapest candidate to dramatically optimize the search space.
* **Minimum Refuel Threshold**: Incorporates a soft penalty constraint (+$1000.0) to avoid unnecessary stop overhead for tiny refuels (less than 5.0 gallons) unless absolutely necessary for feasibility.

---

## 🛠️ Tech Stack
* **Backend**: Django 5.1.15, SQLite
* **Frontend**: HTML5, Vanilla CSS (Premium Slate design), Leaflet.js maps
* **APIs**: OpenStreetMap (OSRM & Nominatim)

---

## 📦 Getting Started

### 1. Installation
Clone the repository and set up a virtual environment:
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. Database Set Up & Migrations
Initialize the SQLite database schema:
```bash
python manage.py migrate
```

### 3. Seed Fuel Station Data
Import and geocode all fuel stations offline from the raw prices CSV and US cities mapping:
```bash
python manage.py import_fuel_stations
```

### 4. Running Tests
Run the comprehensive test suite verifying the DP state, safety reserve calculations, API validation, and the 5-gallon refuel penalty:
```bash
python manage.py test
```

### 5. Start the Application
Run the Django local development server:
```bash
python manage.py runserver
```
Visit `http://127.0.0.1:8000/` in your browser to access the dashboard.

---

## 📡 API Endpoint Reference

### `GET /api/route/`
Calculate the optimal route and refueling schedule.

#### Query Parameters:
* `start` (string, required): Starting city/location in the US (e.g., `Chicago, IL`).
* `finish` (string, required): Destination city/location (e.g., `Houston, TX`).
* `range` (float, optional): Maximum driving range of the vehicle in miles (default: `500.0`).
* `mpg` (float, optional): Fuel efficiency in miles per gallon (default: `10.0`).
* `initial_fuel` (float, optional): Current fuel in gallons at the start. Defaults to a full tank capacity (`range / mpg`).
* `reserve` (float, optional): Safety fuel reserve margin in gallons (default: `3.0`).

#### Sample Success Response:
```json
{
  "success": true,
  "start": {
    "name": "Chicago, IL, USA",
    "lat": 41.8781,
    "lon": -87.6298
  },
  "finish": {
    "name": "Houston, TX, USA",
    "lat": 29.7604,
    "lon": -95.3698
  },
  "route_distance_miles": 1066.6,
  "route_duration_hours": 16.5,
  "total_cost": 154.20,
  "total_fuel_consumed": 106.66,
  "stops": [
    {
      "name": "Pilot Travel Center",
      "city": "Marion",
      "state": "IL",
      "price": 2.899,
      "fuel_bought": 32.5,
      "detour_distance_mi": 1.2
    }
  ]
}
```