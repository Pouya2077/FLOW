"""Point OSMnx at an Overpass server that accepts connections.

overpass-api.de is two machines behind one name. OSMnx looks up a single IP and sends every
request there, so when one machine is unreachable, every query that lands on it waits 180 s and
fails. Searches then fail seemingly at random (seen Oct 2026: gall timed out from our network
while lambert answered). Asking each machine by its own name and using the first that connects
avoids that.
"""

import socket

import osmnx as ox

DEFAULT_URL = "https://overpass-api.de/api"
MACHINES = ["lambert.openstreetmap.de", "gall.openstreetmap.de"]  # what overpass-api.de serves
CONNECT_S = 5  # a reachable machine connects in well under a second


# Call before a run's OSMnx downloads
def use_reachable_server() -> None:
    for host in MACHINES:
        try:
            socket.create_connection((host, 443), timeout=CONNECT_S).close()
        except OSError:
            print(f"Overpass server {host} not reachable")
            continue
        ox.settings.overpass_url = f"https://{host}/api"
        return
    ox.settings.overpass_url = DEFAULT_URL  # neither answered: let OSMnx try the shared name
