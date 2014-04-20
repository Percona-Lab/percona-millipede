#!/usr/bin/python
#
# Copyright (c) 2014, Percona LLC.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import threading
import time
import MySQLdb
import math
import zmq


class DbThread(threading.Thread):
	""" Base DB thread that all others should extend """

	def __init__(self):
		""" Basic constructer - call the parent thread """
		threading.Thread.__init__(self)

	def setupKillEvent(self, eventObj):
		""" Store the threading.Event object to kill the thread """
		self.killEvent = eventObj

	def setupDbConnection(self, dbParams):
		""" Set up the db paramters for connections """

		# DB connection params
		self.dbHost = dbParams['host']
		self.dbUser = dbParams['user']
		self.dbPass = dbParams['pass']
		self.dbName = dbParams['name']
		if dbParams['port']:
			self.dbPort = dbParams['port']
		else:
			self.dbPort = 3306

		# DB error handling
		self.maxRetries = dbParams['numRetries']
		self.retrySleep = dbParams['retrySleep']

		self.refreshConnection()

	def refreshConnection(self):
		""" Connect to the database and keep the handle local and handle retry logic """

		print "[%s] Trying to connect to the db..." % self.name

		numRetries = 0

		while numRetries < self.maxRetries:

			try:
				self.db = MySQLdb.connect(	host = self.dbHost,
											port = self.dbPort,
											user = self.dbUser,
											passwd = self.dbPass,
											db = self.dbName)
				self.db.autocommit(True)
				self.dbHandle = self.db.cursor()
				break

			except Exception, e:
				print "Error in connection attempt: ", e
				numRetries += 1
				time.sleep(self.retrySleep)

		if numRetries == self.maxRetries:
			print "Failed [%d] re-connect attempts, abort thread" % (self.maxRetries,)
			exit()

		print "[%s] Database connection established" % self.name


	def setupServerID(self, serverID):
		""" Set up the server ID for the heartbeat table """
		self.serverID = serverID

	def setupThread(self, threadName):
		""" Set the thread name """
		self.name = threadName
		self.serverName = threadName.split("-")[0]

	def setDelay(self, delay):
		""" Store the main loop delay time """
		self.delay = float(delay)

	def setupStatsd(self, statsConf):
		""" Try to set up the statsd connection (for monitoring) """

		self.statsEnabled = statsConf['enabled']

		try:
			import statsd
			self.statsClient = statsd.StatsClient(
				statsConf['host'],
				int(statsConf['port']),
				statsConf['prefix'])
		except Exception, e:
			pass

	def setupZmq(self, context):
		""" Localize the main context and set up the base Poller """
		self.context = context
		self.poller = zmq.Poller()


class MonitorThread (DbThread):
	""" Monitor thread that connects to a slave and checks replication delay """

	def __init__(self):
		""" Nothing fancy, just build the parent object """
		DbThread.__init__(self)

	def run(self):
		""" Check the delay and retry connection if query fails """

		self.updateSocket = self.context.socket(zmq.SUB)
		self.updateSocket.connect("inproc://update-timestamp")
		self.updateSocket.setsockopt(zmq.SUBSCRIBE, "%s" % (self.serverID))

		self.poller.register(self.updateSocket, zmq.POLLIN)

		updateTracker = 0

		while True:

			if self.killEvent.isSet():
				print "[%s] Caught kill event, so bail..." % (self.name)
				break

			try:
				socks = dict(self.poller.poll(1000))
			except KeyboardInterrupt:
				break
			except Exception, e:
				print e

			if self.updateSocket in socks:

				serverID, timestamp = self.updateSocket.recv_string().split()
				updateTracker += 1

				if updateTracker < self.delay:
					continue

				try:
					self.dbHandle.execute("""SELECT ts FROM heartbeat WHERE server_id = %s""", (self.serverID,))
					row = self.dbHandle.fetchone()
					elapsed = int(math.ceil((float(timestamp) - float(row[0])) * 1000))
					if elapsed > 0:
						print "[%s] Delay (in ms): %d" % (self.name, elapsed)
					updateTracker = 0

					if bool(self.statsEnabled):
						self.statsClient.timing(self.serverName, elapsed)

				except MySQLdb.Error, me:
					print "Caught mysql error: ", me
					self.refreshConnection()

				except Exception, e:
					print "Caught other error", e
					exit()

		print "[%s] Closing the monitor socket" % (self.name)
		self.updateSocket.close()


class UpdateThread (DbThread):
	""" Update thread that connects to a master and sets a heartbeat """

	def __init__(self):
		""" Nothing fancy, just build the parent object """
		DbThread.__init__(self)

	def run(self):
		""" Try to update the current timestamp and retry on failure """

		self.socket = self.context.socket(zmq.PUB)
		self.socket.bind("inproc://update-timestamp")

		while True:

			if self.killEvent.isSet():
				print "[%s] Caught exit event, so bail..." % (self.name)
				break

			strTime = "%9f" % time.time()
			try:
				self.dbHandle.execute("""UPDATE heartbeat SET ts = %s WHERE server_id = %s""", (strTime, self.serverID))
				self.socket.send_string("%s %s" % (self.serverID, strTime))

			except MySQLdb.Error, me:
				print "Caught mysql error: ", me
				self.refreshConnection()

			except Exception, e:
				print "Caught other error", e
				exit()

			time.sleep(self.delay)

		print "Exiting the update thread"
		self.socket.close()

class MainMonitor:
	""" Main object that manages all of the update/select threads """

	def __init__(self, config):
		""" Set up the list of monitor and update threads, as well as statsd conf """

		self.monitorList = []
		self.updateList = []
		self.killEvent = threading.Event()
		self.config = config

		self.context = zmq.Context()
		self.context.setsockopt(zmq.LINGER, 0)

		self.statsConf = {}

		try:

			for item in config.items("statsd"):
				self.statsConf[item[0]] = item[1]
		except Exception as e:
			self.statsConf['enabled'] = 0

	def killThreads(self):
		""" Set the kill event to stop all the child threads """
		self.killEvent.set()

	def parseHostString(self, hostString):
		""" Parse the raw host string to set up the db connection """
		return hostString.split(":")

	def runMonitors(self, hostList):
		""" Initialize and start all of the monitor threads """

		for host in hostList:

			t = self.setupThread(host, "monitor")
			self.monitorList.append(t)

		for t in self.monitorList:
			t.start()

	def runUpdates(self, hostList):
		""" Initialize and start all of the update threads """

		for host in hostList:

			t = self.setupThread(host, "update")
			self.updateList.append(t)

		for t in self.updateList:
			t.start()

	def setupThread(self, host, threadType):
		""" Set up the threads based on the type and host string """

		hostParts = self.parseHostString(host[1])

		if threadType == "monitor":

			t = MonitorThread()
			t.setupThread("%s-monitor" % host[0])
			t.setDelay(self.determineOffset())
			t.setupStatsd(self.statsConf)

		elif threadType == "update":

			t = UpdateThread()
			t.setupThread("%s-update" % host[0])
			t.setDelay(self.config.get("core", "updateDelay"))

		t.setupKillEvent(self.killEvent)
		t.setupServerID(hostParts[1])
		t.setupZmq(self.context)

		dbParams = {}
		dbParams['host'] = hostParts[0]
		dbParams['user'] = self.config.get("dbConn", "user")
		dbParams['pass'] = self.config.get("dbConn", "pwd")
		dbParams['name'] = self.config.get("dbConn", "db")
		if hostParts[2]:
			dbParams['port'] = hostParts[2]
		elif self.config.get("dbConn", "port"):
			dbParams['port'] = self.config.get("dbConn", "port")
		else:
			dbParams['port'] = 3306
		dbParams['numRetries'] = int(self.config.get("dbConn", "numRetries"))
		dbParams['retrySleep'] = float(self.config.get("dbConn", "retrySleep"))

		t.setupDbConnection(dbParams)
		return t

	def determineOffset(self):
		""" Determine how many updates to run prior to running a check on the slave """

		fltUpdate = float(self.config.get("core", "updateDelay"))
		fltMonitor = float(self.config.get("core", "monitorDelay"))

		if fltMonitor < fltUpdate:
			return 1

		return int(fltMonitor / fltUpdate)

	def runSentry(self):
		""" Monitor all the child threads, try to restart failed threads or die if all are stopped """

		while True:

			restartThreads = []
			someRunning = False

			# Loop through all threads and check for alive status, track if one or more is alive
			for t in self.updateList + self.monitorList:

				if t.isAlive():
					someRunning = True
				else:
					restartThreads.append(t)

			# All threads are dead, so just exit out of the monitor loop
			if someRunning is False:
				print "All threads are dead, so just let the process exit"
				break

			# Try to restart the failed threads
			if len(restartThreads) > 0:

				print "Some threads are running, so try to start the others that are dead"
				print restartThreads
				for t in restartThreads:
					t.start()

			time.sleep(1)


if __name__ == "__main__":

	from optparse import OptionParser
	import ConfigParser

	parser = OptionParser()
	parser.add_option("-t", "--type", dest="monitorType", help="monitor|update|both for the type")
	parser.add_option("-c", "--config", dest="configFile", default="./monitor-config.cfg")

	(options, args) = parser.parse_args()

	if options.monitorType is None or options.monitorType not in ["monitor", "update", "all"]:
		print "Must specify [monitor|update|all] for the type"
		exit()


	config = ConfigParser.ConfigParser()
	config.read([options.configFile])

	monitorHosts = []
	updateHosts = []

	for host in config.items("monitorHosts"):
		monitorHosts.append(host)

	for host in config.items("updateHosts"):
		updateHosts.append(host)

	try:
		mon = MainMonitor(config)

		if options.monitorType == "monitor":
			mon.runMonitors(monitorHosts)
		elif options.monitorType == "update":
			mon.runUpdates(updateHosts)
		elif options.monitorType == "all":
			mon.runMonitors(monitorHosts)
			mon.runUpdates(updateHosts)

		mon.runSentry()

	except KeyboardInterrupt:
		print "\nCaught ctl+c, terminating the monitor"
	except Exception as e:
		print "Caught unknown error: ", e
	finally:
		print "Clean up the monitor..."
		mon.killThreads()

	print "...All done"

