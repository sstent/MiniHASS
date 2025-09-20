#!/usr/bin/env python3
"""
Lightweight Smart Home Controller - Docker Edition
A simple Flask app to control TP-Link switches and WebOS TVs
"""

from flask import Flask, render_template, request, jsonify
import asyncio
import json
import socket
import struct
import websockets
import ssl
import requests
from contextlib import asynccontextmanager
import logging
import consul
import json
from threading import Lock
import time
import os
import sys

from aiowebostv import WebOsClient, endpoints as ep

# Add missing endpoint
ep.GET_POWER_STATE = "com.webos.service.tvpower/power/getPowerState"

# Configure logging for container
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'your-secret-key-change-this')

logger = logging.getLogger(__name__)

# Consul configuration
CONSUL_HOST = 'consul.service.dc1.consul'
CONSUL_PORT = 8500
CONSUL_BASE_KEY = 'MiniHass/'

class ConsulConfigManager:
    """Manage configuration using Consul KV store"""
    
    def __init__(self, host=CONSUL_HOST, port=CONSUL_PORT, base_key=CONSUL_BASE_KEY):
        self.client = consul.Consul(host=host, port=port)
        self.base_key = base_key
        
    def get(self, key):
        """Get value from Consul"""
        _, data = self.client.kv.get(f"{self.base_key}{key}")
        return data['Value'].decode() if data else None
        
    def put(self, key, value):
        """Store value in Consul"""
        return self.client.kv.put(f"{self.base_key}{key}", value)
        
    def get_json(self, key):
        """Get JSON value from Consul"""
        value = self.get(key)
        return json.loads(value) if value else None
        
    def put_json(self, key, value):
        """Store JSON value in Consul"""
        return self.put(key, json.dumps(value))

# Initialize Consul config manager
consul_config = ConsulConfigManager()

# Load configuration from Consul
config = consul_config.get_json('config') or {
    'tplink_ip': os.environ.get('TPLINK_IP', '192.168.1.100'),
    'tv_ip': os.environ.get('TV_IP', '192.168.1.101'),
    'tv_mac': os.environ.get('TV_MAC', 'AA:BB:CC:DD:EE:FF')
}

# Config file path for persistence
CONFIG_DIR = '/app/config'
CONFIG_FILE = os.path.join(CONFIG_DIR, 'config.json')


# Device states cache
device_states = {
    'tplink': False,
    'tv': False,
    'last_update': 0
}

# Thread lock for state updates
state_lock = Lock()

class TPLinkDevice:
    """Control TP-Link Kasa devices using the local protocol"""
    
    @staticmethod
    def encrypt(string):
        """Encrypt command for TP-Link protocol"""
        key = 171
        result = struct.pack('>I', len(string))
        for char in string:
            a = key ^ ord(char)
            key = a
            result += bytes([a])
        return result

    @staticmethod
    def decrypt(data):
        """Decrypt response from TP-Link protocol"""
        key = 171
        result = ""
        for byte in data:
            a = key ^ byte
            key = byte
            result += chr(a)
        return result

    @staticmethod
    def send_command(ip, command, port=9999, timeout=5):
        """Send command to TP-Link device"""
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(timeout)
            sock.connect((ip, port))
            sock.send(TPLinkDevice.encrypt(json.dumps(command)))
            
            # Receive response
            data = sock.recv(2048)
            sock.close()
            
            # Skip the first 4 bytes (length header) and decrypt
            response = TPLinkDevice.decrypt(data[4:])
            return json.loads(response)
        except Exception as e:
            logger.error(f"TP-Link command error for {ip}: {e}")
            return None

    @staticmethod
    def get_info(ip):
        """Get device information"""
        command = {"system": {"get_sysinfo": {}}}
        return TPLinkDevice.send_command(ip, command)

    @staticmethod
    def turn_on(ip):
        """Turn on the device"""
        command = {"system": {"set_relay_state": {"state": 1}}}
        return TPLinkDevice.send_command(ip, command)

    @staticmethod
    def turn_off(ip):
        """Turn off the device"""
        command = {"system": {"set_relay_state": {"state": 0}}}
        return TPLinkDevice.send_command(ip, command)


class WebOSTV:
    """Control WebOS TV using aiowebostv library"""
    
    def __init__(self, ip):
        self.ip = ip
        self.consul_key = f'tv_credentials/{ip.replace(".", "_")}'
            
    def load_client_key(self):
        """Load client key from Consul"""
        return consul_config.get(self.consul_key)
    
    def save_client_key(self, key):
        """Save client key to Consul"""
        consul_config.put(self.consul_key, key)
            
    async def _execute_command(self, command):
        """Execute TV command with automatic connection handling"""
        client = None
        try:
            client_key = self.load_client_key()
            logger.debug(f"Attempting WebOS connection to {self.ip} with client key: {client_key}")
            
            # Handle both context manager and manual connection for library compatibility
            client = WebOsClient(self.ip, client_key)
            if hasattr(client, '__aenter__'):
                # Use context manager if available
                async with client as client:
                    return await self._handle_client_commands(client, client_key, command)
            else:
                # Manual connection for older library versions
                await client.connect()
                client.connected = True
                result = await self._handle_client_commands(client, client_key, command)
                await client.disconnect()
                client.connected = False
                return result
                
        except Exception as e:
            logger.error(f"WebOS TV error: {e}", exc_info=True)
            return None
        finally:
            # Ensure cleanup if manual connection was used
            if client and not hasattr(client, '__aenter__') and getattr(client, 'connected', False):
                await client.disconnect()

    async def _handle_client_commands(self, client, original_key, command):
        """Handle TV commands after successful connection"""
        logger.debug(f"Connected to WebOS TV. Client key: {client.client_key}")
        
        # Save new client key if generated
        if client.client_key and client.client_key != original_key:
            logger.info(f"Saving new client key for TV at {self.ip}")
            self.save_client_key(client.client_key)
        
        # Execute requested command using screen-specific methods
        if command == "turn_off":
            logger.debug("Sending turn_off_screen command")
            result = await client.command("request", ep.TURN_OFF_SCREEN)
            logger.debug(f"Turn off command result: {result}, type: {type(result)}")
            return True
        elif command == "turn_on":
            logger.debug("Sending turn_on_screen command")
            await client.command("request", ep.TURN_ON_SCREEN)
            return True
        elif command == "get_power":
            logger.debug("Getting screen state")
            # Use proper method to get screen state
            return await self._get_screen_state(client)
        
        logger.warning(f"Unknown command received: {command}")
        return None
            
    async def turn_screen_off(self):
        """Turn off TV screen"""
        return await self._execute_command("turn_off")
            
    async def turn_screen_on(self):
        """Turn on TV screen"""
        return await self._execute_command("turn_on")
            
    async def get_power_state(self):
        """Get TV screen state"""
        return await self._execute_command("get_power")

    async def _get_screen_state(self, client):
        try:
            result = await client.request(ep.GET_POWER_STATE)
            power_state = result.get('state')
            logger.debug(f"Raw power state: {power_state}")
            
            if power_state in ["Active", "Screen On"]:
                return True
            elif power_state in ["Power Off", "Screen Off"]:
                return False
            return None
        except Exception as e:
            logger.error(f"Error getting screen state: {e}")
            return None


def update_device_state(device, state):
    """Thread-safe device state update"""
    with state_lock:
        device_states[device] = state
        device_states['last_update'] = time.time()


@app.route('/')
def index():
    """Serve the main interface"""
    return render_template('index.html')


@app.route('/health')
def health_check():
    """Health check endpoint for container monitoring"""
    # Check Consul connectivity
    consul_ok = True
    try:
        # Try to get a key to verify connectivity
        consul_config.get('healthcheck')
    except Exception as e:
        logger.error(f"Consul connection error: {e}")
        consul_ok = False
    
    return jsonify({
        'status': 'healthy',
        'timestamp': time.time(),
        'config': {k: v for k, v in config.items() if k != 'tv_mac'},  # Don't expose MAC
        'services': {
            'consul_connected': consul_ok
        }
    })


@app.route('/debug')
def debug_info():
    """Debug information endpoint"""
    import platform
    import sys
    
    debug_data = {
        'flask': {
            'debug_mode': app.debug,
            'testing': app.testing,
            'version': flask.__version__ if 'flask' in globals() else 'unknown'
        },
        'system': {
            'python_version': sys.version,
            'platform': platform.platform(),
            'hostname': socket.gethostname()
        },
        'app': {
            'working_directory': os.getcwd(),
            'template_dir_exists': os.path.exists('templates'),
            'index_template_exists': os.path.exists('templates/index.html'),
            'config_dir_exists': os.path.exists(CONFIG_DIR),
            'config': config
        },
        'files': {
            'templates': [f for f in os.listdir('templates')] if os.path.exists('templates') else [],
            'app_dir': [f for f in os.listdir('.') if not f.startswith('.')]
        },
        'environment': {
            'FLASK_DEBUG': os.environ.get('FLASK_DEBUG', 'not set'),
            'FLASK_ENV': os.environ.get('FLASK_ENV', 'not set'),
            'PYTHONUNBUFFERED': os.environ.get('PYTHONUNBUFFERED', 'not set')
        }
    }
    
    if request.args.get('format') == 'json':
        return jsonify(debug_data)
    else:
        # Return as HTML for easy browser viewing
        html = "<h1>Debug Information</h1><pre>" + json.dumps(debug_data, indent=2, default=str) + "</pre>"
        return html


@app.route('/api/config', methods=['GET', 'POST'])
def handle_config():
    """Handle configuration updates"""
    global config
    
    if request.method == 'POST':
        data = request.get_json()
        config.update(data)
        # Save to Consul
        consul_config.put_json('config', config)
        logger.info("Configuration saved to Consul")
        return jsonify({'status': 'success', 'message': 'Configuration updated and saved to Consul'})
    
    return jsonify(config)


@app.route('/api/status')
def get_status():
    """Get current device states"""
    return jsonify(device_states)


@app.route('/api/tplink/<action>')
def control_tplink(action):
    """Control TP-Link switch"""
    ip = config.get('tplink_ip')
    if not ip:
        return jsonify({'error': 'TP-Link IP not configured'}), 400
    
    try:
        if action == 'on':
            result = TPLinkDevice.turn_on(ip)
            if result and 'system' in result:
                update_device_state('tplink', True)
                return jsonify({'status': 'success', 'state': True})
        elif action == 'off':
            result = TPLinkDevice.turn_off(ip)
            if result and 'system' in result:
                update_device_state('tplink', False)
                return jsonify({'status': 'success', 'state': False})
        elif action == 'status':
            result = TPLinkDevice.get_info(ip)
            if result and 'system' in result:
                state = result['system']['get_sysinfo']['relay_state'] == 1
                update_device_state('tplink', state)
                return jsonify({'status': 'success', 'state': state})
        
        return jsonify({'error': 'Command failed'}), 500
        
    except Exception as e:
        logger.error(f"TP-Link control error: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/tv/<action>')
def control_tv(action):
    """Control WebOS TV"""
    ip = config.get('tv_ip')
    if not ip:
        return jsonify({'error': 'TV IP not configured'}), 400
    
    try:
        tv = WebOSTV(ip)
        
        async def run_command():
            if action == 'screen_on':
                result = await tv.turn_screen_on()
                if result:
                    update_device_state('tv', True)
                    return {'status': 'success', 'state': True}
                else:
                    return {'error': 'Failed to turn screen on'}
            elif action == 'screen_off':
                result = await tv.turn_screen_off()
                if result:
                    update_device_state('tv', False)
                    return {'status': 'success', 'state': False}
                else:
                    return {'error': 'Failed to turn screen off'}
            elif action == 'status':
                result = await tv.get_power_state()
                if result is not None:
                    update_device_state('tv', result)
                    return {'status': 'success', 'state': result}
                else:
                    return {'error': 'Failed to get power state'}
            
            return {'error': 'Invalid action'}
        
        # Run async function
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        result = loop.run_until_complete(run_command())
        loop.close()
        
        if 'error' in result:
            return jsonify(result), 500
        return jsonify(result)
        
    except Exception as e:
        logger.error(f"WebOS TV control error: {e}")
        return jsonify({'error': str(e)}), 500


# HTML template (same as before but stored in templates/index.html)
HTML_TEMPLATE = '''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Smart Home Controller</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
            padding: 20px;
        }
        .container {
            background: rgba(255, 255, 255, 0.95);
            backdrop-filter: blur(10px);
            border-radius: 20px;
            padding: 40px;
            box-shadow: 0 20px 40px rgba(0, 0, 0, 0.1);
            max-width: 400px;
            width: 100%;
            text-align: center;
        }
        h1 {
            color: #333;
            margin-bottom: 40px;
            font-size: 28px;
            font-weight: 600;
        }
        .device {
            margin-bottom: 30px;
            padding: 25px;
            background: linear-gradient(145deg, #f0f0f0, #ffffff);
            border-radius: 15px;
            box-shadow: 0 4px 15px rgba(0, 0, 0, 0.05);
            transition: transform 0.2s ease;
        }
        .device:hover { transform: translateY(-2px); }
        .device-name {
            font-size: 18px;
            font-weight: 600;
            color: #444;
            margin-bottom: 15px;
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 10px;
        }
        .switch-container {
            position: relative;
            display: inline-block;
            width: 80px;
            height: 40px;
        }
        .switch {
            position: absolute;
            cursor: pointer;
            top: 0; left: 0; right: 0; bottom: 0;
            background: linear-gradient(145deg, #ddd, #f1f1f1);
            border-radius: 40px;
            transition: all 0.3s ease;
            box-shadow: inset 0 2px 4px rgba(0, 0, 0, 0.1);
        }
        .switch:before {
            position: absolute;
            content: "";
            height: 32px; width: 32px;
            left: 4px; top: 4px;
            background: linear-gradient(145deg, #fff, #f5f5f5);
            border-radius: 50%;
            transition: all 0.3s ease;
            box-shadow: 0 2px 8px rgba(0, 0, 0, 0.2);
        }
        .switch-input { opacity: 0; width: 0; height: 0; }
        .switch-input:checked + .switch {
            background: linear-gradient(145deg, #4CAF50, #45a049);
        }
        .switch-input:checked + .switch:before {
            transform: translateX(40px);
        }
        .status {
            margin-top: 10px;
            font-size: 14px;
            font-weight: 500;
            padding: 5px 15px;
            border-radius: 20px;
            display: inline-block;
            transition: all 0.3s ease;
        }
        .status.on {
            background: rgba(76, 175, 80, 0.2);
            color: #2e7d32;
        }
        .status.off {
            background: rgba(158, 158, 158, 0.2);
            color: #424242;
        }
        .config-section {
            margin-top: 40px;
            text-align: left;
            background: rgba(0, 0, 0, 0.05);
            padding: 20px;
            border-radius: 10px;
        }
        .input-group { margin-bottom: 15px; }
        .input-group label {
            display: block;
            margin-bottom: 5px;
            font-size: 14px;
            font-weight: 500;
            color: #555;
        }
        .input-group input {
            width: 100%;
            padding: 10px;
            border: 1px solid #ddd;
            border-radius: 8px;
            font-size: 14px;
        }
        .save-btn {
            background: linear-gradient(145deg, #667eea, #764ba2);
            color: white;
            border: none;
            padding: 10px 20px;
            border-radius: 8px;
            cursor: pointer;
            font-size: 14px;
            width: 100%;
        }
        .error { color: #d32f2f; font-size: 12px; margin-top: 5px; }
        .success { color: #2e7d32; font-size: 12px; margin-top: 5px; }
        .docker-info {
            margin-top: 20px;
            padding: 15px;
            background: rgba(103, 126, 234, 0.1);
            border-radius: 10px;
            font-size: 12px;
            color: #667eea;
            text-align: center;
        }
    </style>
</head>
<body>
    <div class="container">
        <h1>🏠 Smart Home</h1>
        
        <div class="device">
            <div class="device-name">
                <span>💡</span>
                TP-Link Switch
            </div>
            <label class="switch-container">
                <input type="checkbox" class="switch-input" id="tplink-switch">
                <span class="switch"></span>
            </label>
            <div class="status off" id="tplink-status">OFF</div>
        </div>

        <div class="device">
            <div class="device-name">
                <span>📺</span>
                LG WebOS TV Screen
            </div>
            <label class="switch-container">
                <input type="checkbox" class="switch-input" id="tv-switch">
                <span class="switch"></span>
            </label>
            <div class="status off" id="tv-status">OFF</div>
        </div>

        <div class="config-section">
            <h3>Device Configuration</h3>
            
            <div class="input-group">
                <label>TP-Link Switch IP</label>
                <input type="text" id="tplink-ip" placeholder="192.168.1.100">
            </div>
            
            <div class="input-group">
                <label>WebOS TV IP</label>
                <input type="text" id="tv-ip" placeholder="192.168.1.101">
            </div>
            
            <button class="save-btn" onclick="saveConfig()">Save Configuration</button>
            <div id="config-message"></div>
            
            <div class="docker-info">
                🐳 Running in Docker Container<br>
                Configuration persists in mounted volume
            </div>
        </div>
    </div>

    <script>
        async function loadConfig() {
            try {
                const response = await fetch('/api/config');
                const config = await response.json();
                document.getElementById('tplink-ip').value = config.tplink_ip || '';
                document.getElementById('tv-ip').value = config.tv_ip || '';
            } catch (error) {
                console.error('Failed to load config:', error);
            }
        }

        async function saveConfig() {
            const config = {
                tplink_ip: document.getElementById('tplink-ip').value,
                tv_ip: document.getElementById('tv-ip').value
            };

            try {
                const response = await fetch('/api/config', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(config)
                });
                
                const result = await response.json();
                showMessage(result.message, 'success');
            } catch (error) {
                showMessage('Failed to save configuration', 'error');
            }
        }

        async function controlDevice(device, action) {
            try {
                const response = await fetch(`/api/${device}/${action}`);
                const result = await response.json();
                
                if (response.ok) {
                    updateStatus(device, result.state);
                    return true;
                } else {
                    alert(`Error: ${result.error}`);
                    return false;
                }
            } catch (error) {
                alert(`Failed to control ${device}`);
                return false;
            }
        }

        function updateStatus(device, isOn) {
            const statusEl = document.getElementById(`${device}-status`);
            statusEl.textContent = isOn ? 'ON' : 'OFF';
            statusEl.className = `status ${isOn ? 'on' : 'off'}`;
        }

        function showMessage(message, type) {
            const messageDiv = document.getElementById('config-message');
            messageDiv.textContent = message;
            messageDiv.className = type;
            setTimeout(() => {
                messageDiv.textContent = '';
                messageDiv.className = '';
            }, 3000);
        }

        document.getElementById('tplink-switch').addEventListener('change', async function() {
            const action = this.checked ? 'on' : 'off';
            const success = await controlDevice('tplink', action);
            if (!success) {
                this.checked = !this.checked;
            }
        });

        document.getElementById('tv-switch').addEventListener('change', async function() {
            const action = this.checked ? 'screen_on' : 'screen_off';
            const success = await controlDevice('tv', action);
            if (!success) {
                this.checked = !this.checked;
            }
        });

        loadConfig();
        
        setInterval(async () => {
            try {
                await controlDevice('tplink', 'status');
                await controlDevice('tv', 'status');
            } catch (error) {
                console.error('Status update failed:', error);
            }
        }, 30000);
    </script>
</body>
</html>'''


def create_template_file():
    """Create the HTML template file"""
    template_dir = 'templates'
    if not os.path.exists(template_dir):
        os.makedirs(template_dir)
    
    template_path = os.path.join(template_dir, 'index.html')
    with open(template_path, 'w') as f:
        f.write(HTML_TEMPLATE)


if __name__ == '__main__':
    # Configuration already loaded from Consul during initialization
    
    # Create template file
    create_template_file()
    
    print("🏠 Smart Home Controller - Docker Edition")
    print("=" * 50)
    print(f"📡 TP-Link IP: {config['tplink_ip']}")
    print(f"📺 TV IP: {config['tv_ip']}")
    print("🌐 Web interface: http://localhost:5000")
    print("❤️  Health check: http://localhost:5000/health")
    print("\n🐳 Container Features:")
    print("  • Persistent configuration and TV credentials storage")
    print("  • Health checks for monitoring")
    print("  • Host network access for device discovery")
    print("  • Automatic container restart")
    print("\n🔧 First time setup:")
    print("  1. On first TV control, accept the pairing prompt on your TV")
    print("  2. TV credentials will be saved automatically for future use")
    print("\n🚀 Starting Flask server...")
    
    # Enable debug mode based on environment
    debug_mode = os.environ.get('FLASK_DEBUG', '0') == '1'
    
    print(f"🚀 Starting Flask server on 0.0.0.0:5000 (debug={debug_mode})")
    app.run(host='0.0.0.0', port=5000, debug=debug_mode, use_reloader=False)
