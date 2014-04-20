percona-millipede
=================

Multi-host, sub-second replication delay monitor.  This tool was developed in conjuction with Vimeo.com.

Description
-----------

Here are the main attributes of the tool:

* Uses zeroMQ PUB/SUB to syncronize the monitor threads to the update thread
* Integrates with statsd for graphing
* Allows you to monitor multiple slaves from a single process

In general, high level goal of this project was to implement a single process that could monitor multiple slaves and graph the delay.  While pt-heartbeat allows for sub-second granularity, we still would've needed a custom wrapper to handle the other pieces and --skew is pretty much the only option in terms of syncronization.  

Here is a summary of the project on mysqlperformanceblog: [percona-millipede – Sub-second replication monitor](http://www.mysqlperformanceblog.com/2014/04/15/percona-millipede-sub-second-replication-monitor/)

Caveats / Warnings
------------------

* Note that this allows you to add significant traffic to your master and slaves(s)
 * This can result in slower recovery, additional slave lag, etc
 * It is recommended to not use anything less than 1 second for extended periods of time unless the hardware can handle the extra load

Prerequisites
-------------

* [zeromq](http://zeromq.org/)
* pyzmq (python binding)
* MySQLdb (python mysql binding)
* statsd (python binding, required for statsd integration)

Installation
------------

Installation is straightforward - simply install the prereqs above, set up a configuration file

Currently, you need to manually set up the heartbeat table and insert the first row (though this is a TODO):

> use percona;  
> CREATE TABLE heartbeat (ts varchar(26) NOT NULL, server_id int(10) unsigned NOT NULL, PRIMARY KEY (server_id));  
> INSERT INTO heartbeat VALUES ('blank-time', SERVER-ID);

Note that **SERVER-ID** should be replaced by the server-id you plan to assign the master

Configuration
-------------

Here is a sample configuration (included):

> [core]
> 
> updateDelay = .01  
> monitorDelay = .1
> 
> [statsd]
> 
> host = statsd.localdomain  
> port = 8125  
> prefix = synthetic_slave_lag.master  
> enabled = 1
> 
> [dbConn]
> 
> user = monitor  
> pwd = password1  
> db = percona  
> numRetries = 10  
> retrySleep = 2
> 
> [monitorHosts]
> 
> slave1 = slave1.localdomain:1  
> slave2 = slave2.localdomain:1  
> slave3 = slave3.localdomain:1  
> slave4 = slave4.localdomain:1
> 
> [updateHosts]
> 
> master1 = master1.localdomain:1

* The **core** section simply set up the delay between checks and updates.
* The **statsd** section sets up the connection to an existing statsd instance
 * Note that if you don't want to use this feature, you should set enabled = 0 and comment out the statsd include (if you don't want to install the package)
* The **dbconn** section is self-explanatory - simply set up the credentials and number of retries (for a failed connection)

Finally, the host sections.  As you would assume, the monitorHosts are slaves and the updateHosts are masters.  Here are the parts to each line:

> slave1 = slave1.localdomain:1:3307

* slave1 = alias name (for graphing and output)
* slave1.localdomain = host to monitor (can also be an IP)
* 1 = the masterID to pair with for a slave, or the serverID to set for a master
* 3307 - optional MySQL port to connect (default 3306)

