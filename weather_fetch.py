import os
import logging
import requests
import json
import sqlite3
import paho.mqtt.client as mqtt
from datetime import datetime
from dotenv import load_dotenv

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

        # MQTT Client
        self.mqtt_client = mqtt.Client()
        # Set MQTT credentials if provided
        mqtt_username = os.getenv('MQTT_USERNAME')
        mqtt_password = os.getenv('MQTT_PASSWORD')
        if mqtt_username and mqtt_password:
            self.mqtt_client.username_pw_set(mqtt_username, mqtt_password)

        try:
            self.mqtt_client.connect(mqtt_broker, mqtt_port)
        except Exception as e:
            logging.error(f"MQTT Connection Error: {e}")
            raise

        # Database Connection
        self.conn = sqlite3.connect(db_path)
        self.cursor = self.conn.cursor()
        self._create_table()

    def _create_table(self):
        """Create weather data table if not exists"""
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS weather_data (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                temperature REAL,
                humidity REAL,
                pressure REAL,
                location TEXT,
                weather_description TEXT,
                device_label TEXT
            )
        ''')
        self.conn.commit()

    def fetch_weather_data(self):
        """Fetch current weather data from OpenWeatherMap API"""
        try:
            response = requests.get(self.current_url, timeout=10)
            response.raise_for_status()  # Raise an exception for bad responses
            return response.json()
        except requests.RequestException as e:
            logging.error(f"API Request Error: {e}")
            return None

    def process_and_store_data(self, response):
        """Process weather data and store in database and MQTT"""
        if not response:
            return

        try:
            timestamp = datetime.utcnow().isoformat()
            data = {
                "temperature": response["main"]["temp"],
                "humidity": response["main"]["humidity"],
                "pressure": response["main"]["pressure"],
                "location": response["name"],
                "weather_description": response["weather"][0]["description"],
                "device_label": "RPi_WeatherStation"
            }

            # Insert into SQLite
            self.cursor.execute(
                """INSERT INTO weather_data 
                (timestamp, temperature, humidity, pressure, location, 
                weather_description, device_label) 
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (timestamp, data["temperature"], data["humidity"],
                 data["pressure"], data["location"],
                 data["weather_description"], data["device_label"])
            )
            self.conn.commit()

            # Publish to MQTT
            self.mqtt_client.publish("/weather/current", json.dumps(data))
            logging.info(f"Weather data for {data['location']} processed successfully")

        except (KeyError, IndexError) as e:
            logging.error(f"Data Processing Error: {e}")
        except sqlite3.Error as e:
            logging.error(f"Database Error: {e}")

    def collect_weather_data(self):
        """Main method to collect and process weather data"""
        weather_response = self.fetch_weather_data()
        self.process_and_store_data(weather_response)

    def close_connections(self):
        """Close database and MQTT connections"""
        try:
            self.mqtt_client.disconnect()
            self.conn.close()
        except Exception as e:
            logging.error(f"Error closing connections: {e}")


def main():
    try:
        # Use environment variables for configuration
        collector = WeatherDataCollector(
            city=os.getenv('WEATHER_CITY', 'Auckland'),
            mqtt_broker=os.getenv('MQTT_BROKER', '192.168.178.153')
        )
        collector.collect_weather_data()
    except Exception as e:
        logging.error(f"Unhandled Error: {e}")
    finally:
        collector.close_connections()


if __name__ == "__main__":
    main()