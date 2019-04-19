# KeyDB Cluster

###how it work

![Cluster](how.jpg)

1) deploy nats (it's simply from helm)
2) deploy keydb-cluster with nats dsn

##known problems

Only 2 keydb pods work correct (wait GA multi master)
