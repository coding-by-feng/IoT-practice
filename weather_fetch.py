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
                device_label TEXT
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
            timestamp = datetime.now(auckland_tz).now().strftime("%Y-%m-%d %H:%M:%S")
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
            logging.info(f"Handled forecast weather data: {forecast_package}")

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
            timestamp = datetime.now(auckland_tz).now().strftime("%Y-%m-%d %H:%M:%S")

            data = {
                "timestamp": timestamp,
                "temperature": response["main"]["temp"],
                "humidity": response["main"]["humidity"],
                "pressure": response["main"]["pressure"],
                "wind_speed": response["wind"]["speed"],
                "location": response["name"],
                "weather_description": response["weather"][0]["description"],
                "device_label": "RPi_WeatherStation"
            }

            # Insert into SQLite
            logging.info(f"Inserting current weather data into database for {data['location']}")
            self.cursor.execute(
                """INSERT INTO weather_data 
                (timestamp, temperature, humidity, pressure, wind_speed, location, 
                weather_description, device_label) 
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (timestamp, data["temperature"], data["humidity"],
                 data["pressure"], data['wind_speed'], data["location"],
                 data["weather_description"], data["device_label"])
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


def main():
    logging.info("Weather data collection script started")
    try:
        # Use environment variables for configuration
        weather_collector = WeatherDataCollector(
            city=os.getenv('WEATHER_CITY', 'Auckland'),
            mqtt_broker=os.getenv('MQTT_BROKER', '192.168.178.153')
        )
        weather_collector.collect_weather_data()
    except Exception as e:
        logging.error(f"Unhandled Error: {e}")
    finally:
        weather_collector.close_connections()
    logging.info("Weather data collection script completed")


if __name__ == "__main__":
    main()