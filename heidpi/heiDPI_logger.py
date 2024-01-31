import argparse
import geoip2.database
import geoip2.errors
import os
import json
import stat
import logging
import datetime
import copy
from multiprocessing.pool import ThreadPool

from heidpi import App
from heidpi import heiDPIsrvd
from heidpi import heiDPI_env

DEFAULT_HOST = '127.0.0.1'
DEFAULT_PORT = 7000
DEFAULT_UNIX = '/tmp/ndpid-distributor.sock'

def dir_path(string):
    if os.path.isdir(string):
        return string
    else:
        raise NotADirectoryError(string)

def file_path(string):
    if os.path.isfile(string):
        return string
    else:
       raise FileNotFoundError(string)

def get_timestamp():
    date_time = datetime.datetime.fromtimestamp(datetime.datetime.now().timestamp())
    return date_time.strftime(LOGGING_CONFIG["datefmt"])

def heidpi_log_event(config_dict, json_dict, additional_processing):
    json_dict_copy = copy.deepcopy(json_dict)
    json_dict_copy['timestamp'] = get_timestamp()
    
    logging.debug("Running additional processing")

    if additional_processing != None:
        additional_processing(config_dict, json_dict_copy)
    
    logging.debug("Finished additional processing")

    ignore_fields = config_dict["ignore_fields"]
    if ignore_fields != []:   
        list(map(json_dict_copy.pop, ignore_fields, [None] * len(ignore_fields)))

    with open(f'{JSON_PATH}/{config_dict["filename"]}.json', "a") as f:
        json.dump(json_dict_copy, f)
        f.write("\n")

def heidpi_flow_processing(config_dict: dict, json_dict: dict):
    if bool(config_dict["geoip2_city"]["enabled"]):
        try:
            response = FLOW_GEOIP2_READER.city(str(json_dict["src_ip"])).raw
            
            json_dict["src_geoip2_city"] = {}
            
            for key in config_dict["geoip2_city"]["keys"]:
                if "." in key:
                    current_data = response
                    try:
                        for key in key.split("."):
                            current_data = current_data[key]
                        json_dict["src_geoip2_city"][key] = current_data
                    except (KeyError, TypeError) as e:
                        logging.exception(f"Exception: {e}")
                else:
                    json_dict["src_geoip2_city"][key] = response[key]
            
        except geoip2.errors.AddressNotFoundError:
            logging.debug(f"No record found for src_ip: {json_dict['src_ip']}")
        except  Exception as e:
            logging.exception(f"Exception: {e}")

        try:
            response = FLOW_GEOIP2_READER.city(str(json_dict["dst_ip"])).raw
            
            json_dict["dst_geoip2_city"] = {}
            
            for key in config_dict["geoip2_city"]["keys"]:
                if "." in key:
                    current_data = response
                    try:
                        for key in key.split("."):
                            current_data = current_data[key]
                        json_dict["dst_geoip2_city"][key] = current_data
                    except (KeyError, TypeError) as e:
                        logging.exception(f"Exception: {e}")
                else:
                    json_dict["dst_geoip2_city"][key] = response[key]
                    
        except geoip2.errors.AddressNotFoundError:
            logging.debug(f"No record found for dst_ip:{json_dict['dst_ip']}")
        except Exception as e:
            logging.exception(f"Exception: {e}")

    # Filter risks, normally applied to flow events
    if "ndpi" in json_dict and "flow_risk" in json_dict["ndpi"] and config_dict["ignore_risks"] != []:   
        list(map(json_dict["ndpi"]["flow_risk"].pop, config_dict["ignore_risks"], [None] * len(config_dict["ignore_risks"])))

def heidpi_worker(address, function):
    nsock = heiDPIsrvd.nDPIsrvdSocket()
    nsock.connect(address)
    nsock.loop(function, None, None)

def heidpi_type_analyzer(json_dict, instance, current_flow, global_user_data):
    if SHOW_FLOW_EVENTS and ("flow_event_id" in json_dict):
        if json_dict["flow_event_name"] in FLOW_CONFIG["flow_event_name"]:
            POOL_FLOW.apply_async(func=heidpi_log_event, args=(FLOW_CONFIG, json_dict, heidpi_flow_processing))
    elif SHOW_PACKET_EVENTS and ("packet_event_id" in json_dict):
        if json_dict["packet_event_name"] in PACKET_CONFIG["packet_event_name"]:
            POOL_PACKET.apply_async(func=heidpi_log_event, args=(PACKET_CONFIG, json_dict, None))
    elif SHOW_DAEMON_EVENTS and ("daemon_event_id" in json_dict):
        if json_dict["daemon_event_name"] in DAEMON_CONFIG["daemon_event_name"]:
            POOL_DAEMON.apply_async(func=heidpi_log_event, args=(DAEMON_CONFIG, json_dict, None))
    elif SHOW_ERROR_EVENTS and ("error_event_id" in json_dict):
        if json_dict["error_event_name"] in ERROR_CONFIG["error_event_name"]:
            POOL_ERROR.apply_async(func=heidpi_log_event, args=(ERROR_CONFIG, json_dict, None))
    return True

def validateAddress(args):
    tcp_addr_set = False
    address = None

    if args.host is None:
        address_tcpip = (DEFAULT_HOST, args.port)
    else:
        address_tcpip = (args.host, args.port)
        tcp_addr_set = True

    if args.unix is None:
        address_unix = DEFAULT_UNIX
    else:
        address_unix = args.unix

    possible_sock_mode = 0
    try:
        possible_sock_mode = os.stat(address_unix).st_mode
    except:
        pass
    if tcp_addr_set == False and stat.S_ISSOCK(possible_sock_mode):
        address = address_unix
    else:
        address = address_tcpip

    return address

def main():
    parser = argparse.ArgumentParser(description='heiDPI Python Interface', formatter_class=argparse.ArgumentDefaultsHelpFormatter)

    group = parser.add_mutually_exclusive_group()
    group.add_argument('--host', type=str, action=heiDPI_env.env_default('HOST'), required=False, help='nDPIsrvd host IP')
    group.add_argument('--unix', type=str, action=heiDPI_env.env_default('UNIX'), required=False, help='nDPIsrvd unix socket path')

    parser.add_argument('--port', type=int, action=heiDPI_env.env_default('PORT'), default=DEFAULT_PORT, help='nDPIsrvd TCP port')

    parser.add_argument('--write', type=dir_path, action=heiDPI_env.env_default('WRITE'), default='/var/log', help='heiDPI write path for logs')

    parser.add_argument('--config', type=file_path, action=heiDPI_env.env_default('CONFIG'), default=f'{os.getcwd()}/config.yml', help='heiDPI write path for logs')

    parser.add_argument('--show-daemon-events', type=int, action=heiDPI_env.env_default('SHOW_DAEMON_EVENTS'), default=0, required=False, help='heiDPI shows daemon events')
    parser.add_argument('--show-packet-events', type=int, action=heiDPI_env.env_default('SHOW_PACKET_EVENTS'), default=0, required=False, help='heiDPI shows packet events')
    parser.add_argument('--show-error-events', type=int, action=heiDPI_env.env_default('SHOW_ERROR_EVENTS'), default=0, required=False, help='heiDPI shows error events')
    parser.add_argument('--show-flow-events', type=int, action=heiDPI_env.env_default('SHOW_FLOW_EVENTS'), default=0, required=False, help='heiDPI shows flow events')

    args = parser.parse_args()
    address = validateAddress(args)

    App(args.config)

    global SHOW_ERROR_EVENTS
    global SHOW_PACKET_EVENTS
    global SHOW_FLOW_EVENTS
    global SHOW_DAEMON_EVENTS
    global JSON_PATH

    SHOW_ERROR_EVENTS = args.show_error_events
    SHOW_PACKET_EVENTS = args.show_packet_events
    SHOW_FLOW_EVENTS = args.show_flow_events
    SHOW_DAEMON_EVENTS = args.show_daemon_events
    JSON_PATH = args.write

    global PACKET_CONFIG
    global FLOW_CONFIG
    global DAEMON_CONFIG
    global ERROR_CONFIG

    PACKET_CONFIG = App.config()["packet_event"].get()
    FLOW_CONFIG = App.config()["flow_event"].get()
    DAEMON_CONFIG = App.config()["daemon_event"].get()
    ERROR_CONFIG = App.config()["error_event"].get()

    global LOGGING_CONFIG
    LOGGING_CONFIG = App.config()["logging"].get()

    logging.info('Recv buffer size: {}'.format(
        heiDPIsrvd.NETWORK_BUFFER_MAX_SIZE))
    logging.info('Connecting to {} ..'.format(
        address[0]+':'+str(address[1]) if type(address) is tuple else address))
    
    # #######################################################################################
    if SHOW_FLOW_EVENTS:
        global POOL_FLOW
        
        POOL_FLOW = ThreadPool(processes=FLOW_CONFIG['threads'])

        if bool(FLOW_CONFIG['geoip2_city']["enabled"]):
            global FLOW_GEOIP2_READER
            FLOW_GEOIP2_READER = geoip2.database.Reader(FLOW_CONFIG['geoip2_city']["filepath"])
        

    # #######################################################################################
    if SHOW_PACKET_EVENTS:
        global POOL_PACKET

        POOL_PACKET = ThreadPool(PACKET_CONFIG['threads'])
        
    # #######################################################################################
    if SHOW_DAEMON_EVENTS:
        global POOL_DAEMON

        POOL_DAEMON = ThreadPool(DAEMON_CONFIG['threads'])

    # #######################################################################################
    if SHOW_ERROR_EVENTS:
        global POOL_ERROR
        
        POOL_ERROR = ThreadPool(ERROR_CONFIG['threads'])
        
    heidpi_worker(address, heidpi_type_analyzer)

if __name__ == '__main__':
    main()
