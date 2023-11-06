### Import Packages

# File Manipulation

import os # For working with Operating System
import sys # System arguments
from dotenv import load_dotenv # Loading .env info

# Web

import requests # Accessing the Web

# Time

import datetime as dt # Working with dates/times
import pytz # Timezones

# Database 

import psycopg2
from psycopg2 import sql

# Data Manipulation

import numpy as np
import geopandas as gpd
import pandas as pd

## Load Functions
import twilio_functions

creds = [os.getenv('DB_NAME'),
         os.getenv('DB_USER'),
         os.getenv('DB_PASS'),
         os.getenv('DB_PORT'),
         os.getenv('DB_HOST')
        ]

pg_connection_dict = dict(zip(['dbname', 'user', 'password', 'port', 'host'], creds)) 

# Functions 

def Users_nearby_sensor(pg_connection_dict, sensor_index, distance):
    '''
    This function will return a list of record_ids from "Sign Up Information" that are within the distance from the sensor
    
    sensor_index = integer
    distance = integer (in meters)
    
    returns record_ids (a list)
    '''
    
    conn = psycopg2.connect(**pg_connection_dict)
    cur = conn.cursor()

    cmd = sql.SQL('''
    WITH sensor as -- query for the desired sensor
    (
    SELECT sensor_index, geometry
    FROM "PurpleAir Stations"
    WHERE sensor_index = {}
    )
    SELECT record_id
    FROM "Sign Up Information" u, sensor s
    WHERE u.subscribed = TRUE AND ST_DWithin(ST_Transform(u.geometry,26915), -- query for users within the distance from the sensor
										    ST_Transform(s.geometry, 26915),{}); 
    ''').format(sql.Literal(sensor_index),
                sql.Literal(distance))

    cur.execute(cmd)

    conn.commit()

    record_ids = [i[0] for i in cur.fetchall()] # Unpack results into list

    # Close cursor
    cur.close()
    # Close connection
    conn.close() 

    return record_ids

# ~~~~~~~~~~~~~~

def Users_to_message_new_alert(pg_connection_dict, record_ids):
    '''
    This function will return a list of record_ids from "Sign Up Information" that have empty active and cached alerts and are in the list or record_ids given
    
    record_ids = a list of ids to check
    
    returns record_ids_to_text (a list)
    '''
    
    conn = psycopg2.connect(**pg_connection_dict)
    cur = conn.cursor()

    cmd = sql.SQL('''
    SELECT record_id
    FROM "Sign Up Information"
    WHERE active_alerts = {} AND cached_alerts = {} AND record_id = ANY ( {} );
    ''').format(sql.Literal('{}'), sql.Literal('{}'), sql.Literal(record_ids))

    cur.execute(cmd)

    conn.commit()

    record_ids_to_text = [i[0] for i in cur.fetchall()]

    # Close cursor
    cur.close()
    # Close connection
    conn.close() 

    return record_ids_to_text
    
# ~~~~~~~~~~~~~~ 
def initialize_report(record_id, reports_for_day, pg_connection_dict):
    '''
    This function will initialize a unique report for a user in the database.

    It will also return the duration_minutes/max_reading of the report
    '''
    
    # Create Cursor for commands
    conn = psycopg2.connect(**pg_connection_dict)
    cur = conn.cursor()

    # Use the record_id to query for the user's cached_alerts
    # Then aggregate from those alerts the start_time, time_difference, max_reading, and nested sensor_indices
    # Unnest the sensor indices into an array of unique sensor_indices
    # Lastly, it will insert all the information into "Reports Archive"
    
    cmd = sql.SQL('''WITH alert_cache as
(
	SELECT cached_alerts
	FROM "Sign Up Information"
	WHERE record_id = {} --inserted record_id
), alerts as
(
	SELECT MIN(p.start_time) as start_time,
			CURRENT_TIMESTAMP AT TIME ZONE 'America/Chicago' 
				- MIN(p.start_time) as time_diff,
			MAX(p.max_reading) as max_reading, 
			ARRAY_AGG(p.sensor_indices) as sensor_indices
	FROM "Archived Alerts Acute PurpleAir" p, alert_cache c
	WHERE p.alert_index = ANY (c.cached_alerts)
), unnested_sensors as 
(
	SELECT ARRAY_AGG(DISTINCT x.v) as sensor_indices
	FROM alerts cross JOIN LATERAL unnest(alerts.sensor_indices) as x(v)
)
INSERT INTO "Reports Archive"
SELECT {}, -- Inserted report_id
        a.start_time, -- start_time
		(((DATE_PART('day', a.time_diff) * 24) + 
    		DATE_PART('hour', a.time_diff)) * 60 + 
		 	DATE_PART('minute', a.time_diff)) as duration_minutes,
			a.max_reading, -- max_reading
		n.sensor_indices,
		c.cached_alerts
FROM alert_cache c, alerts a, unnested_sensors n;
''').format(sql.Literal(record_id),
            sql.Literal(report_id))

    cur.execute(cmd)
    # Commit command
    conn.commit()

    # Now get the information from that report

    cmd = sql.SQL('''SELECT duration_minutes, max_reading
             FROM "Reports Archive"
             WHERE report_id = {};
''').format(sql.Literal(report_id))

    cur.execute(cmd)
    # Commit command
    conn.commit()

    # Unpack response
    duration_minutes, max_reading = cur.fetchall()[0]
    # Close cursor
    cur.close()
    # Close connection
    conn.close()

    return duration_minutes, max_reading
  
# ~~~~~~~~~~~~~~ 
   
def send_all_messages(record_ids, messages):
    '''
    This function will
    1. Send each message to the corresponding record_id
    2. update the user signup data to reflect each new message sent (+1 messages_sent, time added)

    Assumptions: 
    - We won't message the same user twice within an invocation of this function. Otherwise we might need to aggregate the data before step #2
    '''
    numbers = get_phone_numbers(record_ids)
    times = twilio_functions.send_texts(numbers, messages) # this will send all the texts

    update_user_table(record_ids, times)

    return

# ~~~~~~~~~~~~~

def get_phone_numbers(users):
    '''
    takes a list of users and gets associated phone numbers
    
    Assumption: there will always be a valid phone number in REDcap data. Otherwise we would need to add error handling in send_all_messages
    '''

    print("getting numbers!")
    
    numbers = []
    for record_id in users:
        ### needs some contact with REDCap. API? 
        # per ERD shouldn't have any interaction w/ Sign Up Info
        numbers.append(int(record_id*100))
    
    return numbers

# ~~~~~~~~~~~~~

def update_user_table(record_ids, times):
    '''
    Takes a list of users + time objects and updates the "Sign Up Information" table
    to increment each user's messages_sent and last_messaged
    '''
    print("updating Sign Up Information")

    conn = psycopg2.connect(**pg_connection_dict)
    cur = conn.cursor()

    cmd = sql.SQL('''
    SELECT messages_sent
    FROM "Sign Up Information" u
    WHERE u.record_id = ANY ( {} ); 
    ''').format(sql.Literal(record_ids))

    cur.execute(cmd)

    conn.commit()

    messages_sent_list = [i[0] for i in cur.fetchall()] # Unpack results into list
    messages_sent_new = [v+1 for v in messages_sent_list]

    print(record_ids, times, messages_sent_new)
    #2. SQL statement that updates each record (identified by record_ids) with new times, messages_sent_new values
    print("fancy SQL to update the sign up table still pending")
    
    conn.close() 
    conn.close()
    return