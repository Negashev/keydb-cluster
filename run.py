import json
import os
import asyncio

from japronto import Application
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from nats.aio.client import Client as NATS
import aioredis
import time
import socket

IP = os.getenv("MY_POD_IP", socket.gethostbyname(socket.gethostname()))
NATS_DSN = os.getenv("KEYDB_NATS_DSN", "nats://nats:4222")
MY_UUID = os.getenv('KEYDB_UUID', None)
KEYDB_PASSWORD = os.getenv('KEYDB_PASSWORD', None)
if KEYDB_PASSWORD not in [None, ""]:
    KEYDB_PASSWORD = KEYDB_PASSWORD
else:
    KEYDB_PASSWORD = None

READY_TO_BY_PRIMARY = False

async def wait_for_port(port, host='localhost', timeout=5.0):
    """Wait until a port starts accepting TCP connections.
    Args:
        port (int): Port number.
        host (str): Host address on which the port should exist.
        timeout (float): In seconds. How long to wait before raising errors.
    Raises:
        TimeoutError: The port isn't accepting connection after time specified in `timeout`.
    """
    start_time = time.perf_counter()
    print('waiting local redis')
    while True:
        try:
            with socket.create_connection((host, port), timeout=timeout):
                break
        except OSError as ex:
            time.sleep(1)
            if time.perf_counter() - start_time >= timeout:
                print('Waited too long for the port {} on host {} to start accepting '
                                   'connections.'.format(port, host))
    print('local redis is up')
    time.sleep(1)
    while True:
        try:
            conn = await aioredis.create_redis(f'redis://{IP}', password=KEYDB_PASSWORD, timeout=10)
            val = await conn.info('replication')
            replication = val['replication']
            conn.close()
            await conn.wait_closed()
            break
        except Exception as e:
            time.sleep(1)
    print('local redis is ready')

async def get_replication(request):
    global KEYDB_PASSWORD
    global IP
    global READY_TO_BY_PRIMARY
    conn = await aioredis.create_redis(f'redis://{IP}', password=KEYDB_PASSWORD, timeout=10)
    val = await conn.info('replication')
    replication = val['replication']
    conn.close()
    await conn.wait_closed()
    code = 200
    if not READY_TO_BY_PRIMARY:
        code = 400
    return request.Response(json=replication, code=code)


async def new_server(msg):
    global IP
    global MY_UUID
    global READY_TO_BY_PRIMARY
    new_server = json.loads(msg.data.decode())
    if new_server['MY_UUID'] == MY_UUID:
        return
    if not READY_TO_BY_PRIMARY:
        return
    print(f"two bind {new_server['MY_UUID']} ({new_server['IP']})<-->{MY_UUID} ({IP})")
    # add_replicaof fo external
    await nc.publish(new_server['MY_UUID'], bytes(IP, 'utf-8'))
    await asyncio.sleep(int(os.getenv("KEYDB_REPLICAOF_SLEEP", "10")))
    # add_replicaof fo me
    await nc.publish(MY_UUID, bytes(new_server['IP'], 'utf-8'))


async def add_replicaof(msg):
    global KEYDB_PASSWORD
    global IP
    global READY_TO_BY_PRIMARY
    master_ip = msg.data.decode()
    # don't make local replica
    conn = await aioredis.create_redis(f'redis://{IP}', password=KEYDB_PASSWORD, timeout=10)
    set_replicaof = True
    # check if master_ip exist
    replication = await conn.execute('info', 'replication')
    for line in replication.decode().splitlines():
        if line.startswith('master_host'):
            master_host, ip = line.split(':')
            if ip == master_ip:
                set_replicaof = False
                print('replicaof', master_ip, 6379, "not set, Already exist")
                break
    # start replicaof
    if set_replicaof:
        replicaof = await conn.execute('REPLICAOF', master_ip, 6379)
        READY_TO_BY_PRIMARY = False
        print('replicaof', master_ip, 6379, replicaof.decode())
    conn.close()
    await conn.wait_closed()


async def get_keydb_id(app):
    global MY_UUID
    global KEYDB_PASSWORD
    global IP
    if MY_UUID is not None:
        return MY_UUID
    conn = await aioredis.create_redis(f'redis://{IP}', password=KEYDB_PASSWORD, loop=app.loop, timeout=10)
    val = await conn.info('server')
    MY_UUID = 'keydb-cluster-' + val['server']['run_id']
    conn.close()
    await conn.wait_closed()


async def connect_nats(app):
    global KEYDB_PASSWORD
    global NATS_DSN
    global MY_UUID
    global IP
    await nc.connect(servers=[NATS_DSN], io_loop=app.loop, max_reconnect_attempts=-1, verbose=True)
    print(MY_UUID)
    await nc.subscribe(MY_UUID, cb=add_replicaof)
    await nc.subscribe('new_server', cb=new_server)
    # await nc.publish('new_server', bytes(json.dumps({"MY_UUID": MY_UUID, "IP": IP}), 'utf-8'))
    # add nats to japronto app
    app.extend_request(lambda x: nc, name='nc', property=True)


async def ping_primary():
    global MY_UUID
    global IP
    global READY_TO_BY_PRIMARY
    conn = await aioredis.create_redis(f'redis://{IP}', password=KEYDB_PASSWORD, timeout=10)
    # check if master sync
    master_sync_left_bytes_status = True
    master_ip = 'master'
    replication = await conn.execute('info', 'replication')
    for line in replication.decode().splitlines():
        if line.startswith('master_host'):
            master_host, master_ip = line.split(':')
        if line.startswith('master_sync_left_bytes'):
            master_sync_left_bytes, left_bytes = line.split(':')
            left_bytes = int(left_bytes)
            if left_bytes != 0:
                print(f'Server sync with {master_ip} not ready master_sync_left_bytes={left_bytes}')
                master_sync_left_bytes_status = False
    conn.close()
    await conn.wait_closed()
    READY_TO_BY_PRIMARY = master_sync_left_bytes_status
    if READY_TO_BY_PRIMARY:
        await nc.publish('new_server', bytes(json.dumps({"MY_UUID": MY_UUID, "IP": IP}), 'utf-8'))


async def connect_scheduler():
    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(ping_primary, 'interval', seconds=10)
    scheduler.start()


print(IP)
app = Application()
nc = NATS()
app.loop.run_until_complete(wait_for_port(6379))
app.loop.run_until_complete(get_keydb_id(app))
app.loop.run_until_complete(connect_nats(app))
app.loop.run_until_complete(connect_scheduler())
r = app.router
r.add_route('/', get_replication)

app.run()
