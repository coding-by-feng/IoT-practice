import sys
import os
import logging
import requests
import json
import sqlite3
import paho.mqtt.client as mqtt
from datetime import datetime
from dotenv import load_dotenv
import pytz

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('weather_app.log'),
        logging.StreamHandler()
    ]
)


class WeatherDataCollector:
    def __init__(self, city, mqtt_broker, mqtt_port=1883, db_path='weather.db'):
        # Load environment variables
        load_dotenv()

        # Configuration
        self.city = city
        self.api_key = os.getenv('OPENWEATHER_API_KEY')
        if not self.api_key:
            raise ValueError("OpenWeather API key not found in environment variables")

        # URLs
        self.current_url = f"https://api.openweathermap.org/data/2.5/weather?q={self.city}&appid={self.api_key}&units=metric"
        self.forecast_url = f"https://api.openweathermap.org/data/2.5/forecast?q={self.city}&appid={self.api_key}&units=metric&cnt=48"

        # MQTT Client
        self.mqtt_client = mqtt.Client()
        # Set MQTT credentials if provided
        mqtt_username = os.getenv('MQTT_USERNAME')
        mqtt_password = os.getenv('MQTT_PASSWORD')
        if mqtt_username and mqtt_password:
            self.mqtt_client.username_pw_set(mqtt_username, mqtt_password)

        try:
            self.mqtt_client.connect(mqtt_broker, mqtt_port)
            logging.info(f"Connected to MQTT broker at {mqtt_broker}:{mqtt_port}")
        except Exception as e:
            logging.error(f"MQTT Connection Error: {e}")
            raise

        # Database Connection
        self.conn = sqlite3.connect(db_path)
        self.cursor = self.conn.cursor()
        self._create_table()

        logging.info(f"WeatherDataCollector initialized for city: {self.city}, MQTT broker: {mqtt_broker}")

    def _create_table(self):
        """Create weather data tables if not exists"""
        logging.info("Checking/creating weather data tables in database")

        # Table for current weather
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS weather_data (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                temperature REAL,
                humidity REAL,
                pressure REAL,
                wind_speed REAL,
                location TEXT,
                weather_description TEXT,
                device_label TEXT,
                rain_1h REAL,
                snow_1h REAL,
                clouds_percent INTEGER,
                visibility INTEGER
            )
        ''')

        # Table for forecast data - added clouds, rain, snow, visibility columns
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS weather_forecast (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                forecast_time TEXT,
                temperature REAL,
                humidity REAL,
                pressure REAL,
                location TEXT,
                weather_description TEXT,
                wind_speed REAL,
                clouds_percent INTEGER,
                rain_3h REAL,
                snow_3h REAL,
                visibility INTEGER,
                device_label TEXT
            )
        ''')

        self.conn.commit()
        logging.info("Weather data tables checked/created in database")

    def fetch_weather_data(self):
        """Fetch current weather data from OpenWeatherMap API"""
        logging.info(f"Fetching current weather data for {self.city}")
        try:
            response = requests.get(self.current_url, timeout=10)
            response.raise_for_status()  # Raise an exception for bad responses
            logging.info(f"Current weather data fetched successfully for {self.city}")
            return response.json()
        except requests.RequestException as e:
            logging.error(f"Current API Request Error: {e}")
            return None

    def fetch_forecast_data(self):
        """Fetch 48-hour forecast data from OpenWeatherMap API"""
        logging.info(f"Fetching 48-hour forecast data for {self.city}")
        try:
            response = requests.get(self.forecast_url, timeout=10)
            response.raise_for_status()  # Raise an exception for bad responses
            logging.info(f"Forecast data fetched successfully for {self.city}")
            return response.json()
        except requests.RequestException as e:
            logging.error(f"Forecast API Request Error: {e}")
            return None

    def process_and_store_forecast_data(self, response):
        """Process forecast data and store in database and MQTT"""
        if not response:
            logging.warning("No forecast data to process")
            return

        try:
            # Use a fixed timestamp for data collection time
            # Set Auckland time zone
            auckland_tz = pytz.timezone('Pacific/Auckland')
            # Get current time in Auckland
            timestamp = datetime.now(auckland_tz).strftime("%Y-%m-%d %H:%M:%S")
            city_name = response["city"]["name"]
            forecast_list = response["list"]

            logging.info(f"Processing {len(forecast_list)} hourly forecast entries for {city_name}")

            # Process each forecast period
            forecast_data_list = []
            for forecast in forecast_list:
                forecast_time = forecast["dt_txt"]
                
                # Extract rain and snow values (which might not be present in all forecasts)
                rain_3h = forecast.get("rain", {}).get("3h", 0)
                snow_3h = forecast.get("snow", {}).get("3h", 0)
                
                # Extract clouds percentage and visibility
                clouds_percent = forecast.get("clouds", {}).get("all", 0)
                visibility = forecast.get("visibility", 0)
                
                forecast_item = {
                    "forecast_time": forecast_time,
                    "temperature": forecast["main"]["temp"],
                    "humidity": forecast["main"]["humidity"],
                    "pressure": forecast["main"]["pressure"],
                    "location": city_name,
                    "weather_description": forecast["weather"][0]["description"],
                    "wind_speed": forecast["wind"]["speed"],
                    "clouds_percent": clouds_percent,
                    "rain_3h": rain_3h,
                    "snow_3h": snow_3h,
                    "visibility": visibility,
                    "device_label": "RPi_WeatherStation"
                }
                forecast_data_list.append(forecast_item)

                # Insert each forecast period into database
                self.cursor.execute(
                    """INSERT INTO weather_forecast 
                    (forecast_time, temperature, humidity, pressure, location, 
                    weather_description, wind_speed, clouds_percent, rain_3h, snow_3h, visibility, device_label) 
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (forecast_time, forecast_item["temperature"],
                     forecast_item["humidity"], forecast_item["pressure"],
                     forecast_item["location"], forecast_item["weather_description"],
                     forecast_item["wind_speed"], forecast_item["clouds_percent"],
                     forecast_item["rain_3h"], forecast_item["snow_3h"],
                     forecast_item["visibility"], forecast_item["device_label"])
                )

            self.conn.commit()

            # Publish the entire forecast as a single JSON message
            logging.info(f"Publishing forecast data to MQTT for {city_name}")
            forecast_package = {
                "location": city_name,
                "timestamp": timestamp,
                "forecast_count": len(forecast_data_list),
                "forecast_items": forecast_data_list
            }
            self.mqtt_client.publish("/weather/forecast", json.dumps(forecast_package))
            logging.info(f"Forecast data for {city_name} processed successfully")

        except (KeyError, IndexError) as e:
            logging.error(f"Forecast Data Processing Error: {e}")
        except sqlite3.Error as e:
            logging.error(f"Database Error for forecast data: {e}")

    def process_and_store_data(self, response):
        """Process current weather data and store in database and MQTT"""
        if not response:
            logging.warning("No current weather data to process")
            return

        logging.info(f"Raw current weather data: {response}")

        try:
            # Use a fixed timestamp format for consistency
            # Set Auckland time zone
            auckland_tz = pytz.timezone('Pacific/Auckland')
            # Get current time in Auckland
            timestamp = datetime.now(auckland_tz).strftime("%Y-%m-%d %H:%M:%S")

            # Extract rain and snow values (which might not be present)
            # OpenWeatherMap API provides rain and snow in a nested structure
            rain_1h = response.get("rain", {}).get("1h", 0)
            snow_1h = response.get("snow", {}).get("1h", 0)
            
            # Extract clouds percentage and visibility
            clouds_percent = response.get("clouds", {}).get("all", 0)
            visibility = response.get("visibility", 0)

            data = {
                "timestamp": timestamp,
                "temperature": response["main"]["temp"],
                "humidity": response["main"]["humidity"],
                "pressure": response["main"]["pressure"],
                "wind_speed": response["wind"]["speed"],
                "location": response["name"],
                "weather_description": response["weather"][0]["description"],
                "device_label": "RPi_WeatherStation",
                "rain_1h": rain_1h,
                "snow_1h": snow_1h,
                "clouds_percent": clouds_percent,
                "visibility": visibility
            }

            # Insert into SQLite
            logging.info(f"Inserting current weather data into database for {data['location']}")
            self.cursor.execute(
                """INSERT INTO weather_data 
                (timestamp, temperature, humidity, pressure, wind_speed, location, 
                weather_description, device_label, rain_1h, snow_1h, clouds_percent, visibility) 
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (timestamp, data["temperature"], data["humidity"],
                data["pressure"], data['wind_speed'], data["location"],
                data["weather_description"], data["device_label"], data["rain_1h"],
                data["snow_1h"], data["clouds_percent"], data["visibility"])
            )
            self.conn.commit()

            # Publish to MQTT
            logging.info(f"Publishing current weather data to MQTT for {data['location']}")
            self.mqtt_client.publish("/weather/current", json.dumps(data))
            logging.info(f"Current weather data for {data['location']} processed successfully")

        except (KeyError, IndexError) as e:
            logging.error(f"Current Data Processing Error: {e}")
        except sqlite3.Error as e:
            logging.error(f"Database Error for current data: {e}")

    def collect_weather_data(self):
        """Main method to collect and process current and forecast weather data"""
        logging.info("Starting weather data collection")

        # Collect current weather data
        weather_response = self.fetch_weather_data()
        self.process_and_store_data(weather_response)

        # Collect forecast data
        forecast_response = self.fetch_forecast_data()
        self.process_and_store_forecast_data(forecast_response)
        
        # Analyze and publish alarms based on collected data
        if weather_response and forecast_response:
            self.analyze_and_publish_alarms(weather_response, forecast_response)
        else:
            logging.warning("Cannot analyze weather alarms due to missing data")

        logging.info("Weather data collection completed")

    def close_connections(self):
        """Close database and MQTT connections"""
        try:
            logging.info("Disconnecting from MQTT broker")
            self.mqtt_client.disconnect()
            logging.info("Disconnected from MQTT broker")
            logging.info("Closing database connection")
            self.conn.close()
            logging.info("Database connection closed")
        except Exception as e:
            logging.error(f"Error closing connections: {e}")

    def analyze_and_publish_alarms(self, current_data, forecast_data):
        """
        Analyze weather data for high-risk conditions and publish alarms
        
        Args:
            current_data: Current weather data from API
            forecast_data: Forecast weather data from API
        """
        logging.info("Analyzing weather data for potential risks and health alarms")
        
        if not current_data or not forecast_data:
            logging.warning("Incomplete data for alarm analysis")
            return
        
        alarms = []
        location = current_data["name"]
        
        # Set Auckland time zone
        auckland_tz = pytz.timezone('Pacific/Auckland')
        # Get current time in Auckland
        timestamp = datetime.now(auckland_tz).strftime("%Y-%m-%d %H:%M:%S")
        
        # ----- Current Weather Alarms -----
        
        # Temperature alarms - extreme heat
        if current_data["main"]["temp"] > 37:
            alarms.append({
                "type": "EXTREME_HEAT",
                "severity": "high",
                "message": f"Extreme heat warning: Temperature at {current_data['main']['temp']}°C",
                "health_impact": "Risk of heat stroke and dehydration, stay hydrated and avoid sun exposure"
            })
        elif current_data["main"]["temp"] > 35:
            alarms.append({
                "type": "HIGH_TEMPERATURE",
                "severity": "medium",
                "message": f"High temperature alert: {current_data['main']['temp']}°C",
                "health_impact": "Stay hydrated and avoid prolonged sun exposure"
            })
        
        # Temperature alarms - extreme cold
        if current_data["main"]["temp"] < 0:
            alarms.append({
                "type": "FREEZING",
                "severity": "high",
                "message": f"Freezing temperature warning: {current_data['main']['temp']}°C",
                "health_impact": "Risk of hypothermia and frostbite, stay warm and limit time outdoors"
            })
        elif current_data["main"]["temp"] < 5:
            alarms.append({
                "type": "LOW_TEMPERATURE",
                "severity": "medium",
                "message": f"Low temperature alert: {current_data['main']['temp']}°C",
                "health_impact": "Dress warmly and be cautious of icy conditions"
            })
        
        # Wind alarms
        if current_data["wind"]["speed"] > 20:
            alarms.append({
                "type": "STRONG_WIND",
                "severity": "high",
                "message": f"Strong wind warning: {current_data['wind']['speed']} m/s",
                "health_impact": "Danger from flying debris, secure loose objects and avoid outdoor activities"
            })
        elif current_data["wind"]["speed"] > 10:
            alarms.append({
                "type": "MODERATE_WIND",
                "severity": "medium",
                "message": f"Moderate wind alert: {current_data['wind']['speed']} m/s",
                "health_impact": "Use caution when outdoors, especially for the elderly"
            })
        
        # Rain alarms
        rain_1h = current_data.get("rain", {}).get("1h", 0)
        if rain_1h > 10:
            alarms.append({
                "type": "HEAVY_RAIN",
                "severity": "high",
                "message": f"Heavy rain warning: {rain_1h} mm in last hour",
                "health_impact": "Potential for flooding, avoid flooded areas and driving in heavy rain"
            })
        elif rain_1h > 5:
            alarms.append({
                "type": "MODERATE_RAIN",
                "severity": "medium",
                "message": f"Moderate rain alert: {rain_1h} mm in last hour",
                "health_impact": "Reduced visibility while driving, bring umbrella and waterproof clothing"
            })
        
        # Visibility alarms
        visibility = current_data.get("visibility", 10000) / 1000  # Convert to km
        if visibility < 1:
            alarms.append({
                "type": "VERY_LOW_VISIBILITY",
                "severity": "high",
                "message": f"Very low visibility warning: {visibility} km",
                "health_impact": "Extremely dangerous driving conditions, avoid travel if possible"
            })
        elif visibility < 5:
            alarms.append({
                "type": "LOW_VISIBILITY",
                "severity": "medium",
                "message": f"Low visibility alert: {visibility} km",
                "health_impact": "Use caution when driving and allow extra travel time"
            })
        
        # UV index - not available in your data, consider adding to API call if possible
        
        # Air quality - not available in current API, consider adding an air quality API
        
        # ----- Forecast Weather Alarms -----
        
        # Analyze forecast for upcoming severe weather
        forecast_list = forecast_data["list"]
        
        # Temperature forecasts
        max_temp_24h = max([f["main"]["temp"] for f in forecast_list[:8]])
        min_temp_24h = min([f["main"]["temp"] for f in forecast_list[:8]])
        
        if max_temp_24h > 35:
            alarms.append({
                "type": "FORECAST_EXTREME_HEAT",
                "severity": "high",
                "message": f"Extreme heat forecast in next 24 hours: Up to {max_temp_24h}°C expected",
                "health_impact": "Prepare for extreme heat conditions, ensure cooling and hydration"
            })
        
        if min_temp_24h < 0:
            alarms.append({
                "type": "FORECAST_FREEZING",
                "severity": "high",
                "message": f"Freezing temperatures forecast in next 24 hours: Down to {min_temp_24h}°C expected",
                "health_impact": "Prepare for freezing conditions, ensure heating and warm clothing"
            })
        
        # Heavy rain forecasts
        rain_forecasts = [f.get("rain", {}).get("3h", 0) for f in forecast_list[:8]]
        if any(rain > 20 for rain in rain_forecasts):
            alarms.append({
                "type": "FORECAST_HEAVY_RAIN",
                "severity": "high",
                "message": "Heavy rain forecast in next 24 hours",
                "health_impact": "Prepare for potential flooding and travel disruptions"
            })
        
        # Strong wind forecasts
        wind_forecasts = [f["wind"]["speed"] for f in forecast_list[:8]]
        if any(wind > 20 for wind in wind_forecasts):
            alarms.append({
                "type": "FORECAST_STRONG_WIND",
                "severity": "high",
                "message": "Strong winds forecast in next 24 hours",
                "health_impact": "Secure loose objects and prepare for potential power outages"
            })
        
        # Check for extreme weather combinations (e.g., heavy rain + strong wind)
        # This could indicate more severe storms
        has_heavy_rain = any(rain > 10 for rain in rain_forecasts)
        has_strong_wind = any(wind > 15 for wind in wind_forecasts)
        
        if has_heavy_rain and has_strong_wind:
            alarms.append({
                "type": "FORECAST_STORM",
                "severity": "high",
                "message": "Storm conditions (heavy rain and strong winds) forecast in next 24 hours",
                "health_impact": "Stay indoors if possible, avoid travel, and prepare for power outages"
            })
        
        # If we have alarms, publish them

        logging.info(f"alarms: {alarms}")
        
        if alarms:
            alarm_data = {
                "location": location,
                "timestamp": timestamp,
                "alarm_count": len(alarms),
                "alarms": alarms
            }
            
            logging.info(f"Publishing {len(alarms)} weather alarms to MQTT")
            self.mqtt_client.publish("/weather/alarms", json.dumps(alarm_data))
            logging.info("Weather alarms published successfully")
        else:
            logging.info("No weather alarms detected")

# Load the JSON files
def load_mock_data():
    try:
        # Load current weather mock data
        with open('current_alarms_mock.json', 'r') as current_file:
            weather_response = json.load(current_file)
            
        # Load forecast mock data
        with open('forecast_alarms_mock.json', 'r') as forecast_file:
            forecast_response = json.load(forecast_file)
            
        print("Mock data loaded successfully!")
        return weather_response, forecast_response
    
    except FileNotFoundError as e:
        print(f"Error: File not found - {e}")
        return None, None
    
    except json.JSONDecodeError as e:
        print(f"Error: Invalid JSON format - {e}")
        return None, None
    
    except Exception as e:
        print(f"Unexpected error loading mock data: {e}")
        return None, None

# Testing code
def test_weather_alarms():
    # Initialize your weather collector
    weather_collector = WeatherDataCollector(
        city=os.getenv('WEATHER_CITY', 'Auckland'),
        mqtt_broker=os.getenv('MQTT_BROKER', 'comitup-401.local')
    )
    
    # Load the mock data from JSON files
    weather_response, forecast_response = load_mock_data()
    
    # Check if data was loaded successfully
    if weather_response and forecast_response:
        print("Analyzing and publishing alarms based on mock data...")
        # Instead of fetching from API, use our mock data
        weather_collector.analyze_and_publish_alarms(weather_response, forecast_response)
        print("Alarm analysis and publishing completed!")
    else:
        print("Cannot analyze weather alarms due to missing or invalid mock data")
    
    # Close connections
    weather_collector.close_connections()

def main():
    logging.info("Weather data collection script started")
    try:
        # Use environment variables for configuration
        weather_collector = WeatherDataCollector(
            city=os.getenv('WEATHER_CITY', 'Auckland'),
            mqtt_broker=os.getenv('MQTT_BROKER', 'comitup-401.local')
        )
        weather_collector.collect_weather_data()
    except Exception as e:
        logging.error(f"Unhandled Error: {e}")
    finally:
        weather_collector.close_connections()
    logging.info("Weather data collection script completed")


# Check command-line arguments to determine which function to run
if __name__ == "__main__":
    # Configure logging (if not already done at the top of your script)
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler('weather_app.log'),
            logging.StreamHandler()
        ]
    )
    
    # Check if "test" is passed as a command-line argument
    if len(sys.argv) > 1 and sys.argv[1].lower() == "test":
        logging.info("Running in test mode with mock data")
        test_weather_alarms()
    else:
        logging.info("Running in normal operation mode")
        main()
