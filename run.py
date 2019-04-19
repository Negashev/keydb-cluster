import json
import os

from japronto import Application
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from nats.aio.client import Client as NATS
import aioredis
import socket

IP = os.getenv("MY_POD_IP", socket.gethostbyname(socket.gethostname()))
NATS_DSN = os.getenv("KEYDB_NATS_DSN", "nats://nats:4222")
MY_UUID = os.getenv('KEYDB_UUID', None)
KEYDB_PASSWORD = os.getenv('KEYDB_PASSWORD', None)
if KEYDB_PASSWORD not in [None, ""]:
    KEYDB_PASSWORD = f"{KEYDB_PASSWORD}@"
else:
    KEYDB_PASSWORD = ""


async def get_replication(request):
    global KEYDB_PASSWORD
    global IP
    conn = await aioredis.create_redis(f'redis://{KEYDB_PASSWORD}{IP}')
    val = await conn.info('replication')
    replication = val['replication']
    conn.close()
    await conn.wait_closed()
    return request.Response(json=replication)


async def new_server(msg):
    global IP
    global MY_UUID
    new_server = json.loads(msg.data.decode())
    if new_server['MY_UUID'] == MY_UUID:
        return
    print(f"two bind {new_server['MY_UUID']} ({new_server['IP']})<-->{MY_UUID} ({IP})")
    # add_replicaof fo external
    await nc.publish(new_server['MY_UUID'], bytes(IP, 'utf-8'))
    # add_replicaof fo me
    await nc.publish(MY_UUID, bytes(new_server['IP'], 'utf-8'))


async def add_replicaof(msg):
    global KEYDB_PASSWORD
    global IP
    master_ip = msg.data.decode()
    # don't make local replica
    conn = await aioredis.create_redis(f'redis://{KEYDB_PASSWORD}{IP}')
    replicaof = await conn.execute('REPLICAOF', master_ip, 6379)
    print('replicaof', master_ip, 6379, replicaof.decode())
    conn.close()
    await conn.wait_closed()


async def get_keydb_id(app):
    global MY_UUID
    global KEYDB_PASSWORD
    global IP
    if MY_UUID is not None:
        return MY_UUID
    conn = await aioredis.create_redis(f'redis://{KEYDB_PASSWORD}{IP}', loop=app.loop)
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
    await nc.publish('new_server', bytes(json.dumps({"MY_UUID": MY_UUID, "IP": IP}), 'utf-8'))
    # add nats to japronto app
    app.extend_request(lambda x: nc, name='nc', property=True)

# TODO Remove offline redis master
# async def connect_scheduler():
#     scheduler = AsyncIOScheduler(timezone="UTC")
#     scheduler.add_job(remove_slave, 'interval', seconds=30)
#     scheduler.start()

print(IP)
app = Application()
nc = NATS()
app.loop.run_until_complete(get_keydb_id(app))
app.loop.run_until_complete(connect_nats(app))
# app.loop.run_until_complete(connect_scheduler())
r = app.router
r.add_route('/', get_replication)

app.run()