import struct, time, Queue
from operator import itemgetter
from pyccn import CCN, Closure, ContentObject, Interest, Name, Key

def packet(name, data, key):
	co = ContentObject.ContentObject()
	co.name = Name.Name(name)
	co.content = data
	co.signedInfo.publisherPublicKeyDigest = key.publicKeyID
	co.signedInfo.keyLocator = Key.KeyLocator(key)
	co.sign(key)
	return co

class RingBuffer:
	def __init__(self, size):
		self.data = [None for i in xrange(size)]

	def append(self, x):
		self.data.pop(0)
		self.data.append(x)

	def get(self):
		return self.data

class CCNBuffer(Queue.Queue):
	def _init(self, maxsize):
		self.queue = []

	def _qsize(self):
		return len(self.queue)

	def _get(self):
		raise NotImplementedError()

	def _put(self, item):
		if not isinstance(item, ContentObject.ContentObject):
			raise ValueError("Item needs to be of ContentObject type")

#		if self._qsize() >= self.maxsize:
#			self._get_old(10)

#		if item in self.queue:
#			raise ValueError("Item %s is already in the buffer" % item)
#		item_name = Name.Name(item.name)
#		name = str(item_name)
		self.queue.append((time.time(), item))

		if self._qsize() >= self.maxsize:
			return self.queue.pop(0)[1]

		return None

	def _get_element(self, interest):
		#self.queue.sort(key=itemgetter(1))

		for co in enumerate(self.queue):
			if co[1][1].matchesInterest(interest):
				return self.queue.pop(co[0])[1]

		for co in enumerate(self.queue):
			print "%d %s" % (co[0], co[1][1].name)

		raise ValueError("Element '%s' not found in the queue" % interest.name)

	def _get_old(self, diff):
		now = time.time()
		then = now - diff

		ret = []
		new = []
		for co in self.queue:
			if co[0] < then:
				ret.append(co[1])
			else:
				new.append(co)

		self.queue = new

		return ret

	def put(self, item, block=True, timeout=None):
		"""Put an item into the queue.

		If optional args 'block' is true and 'timeout' is None (the default),
		block if necessary until a free slot is available. If 'timeout' is
		a positive number, it blocks at most 'timeout' seconds and raises
		the Full exception if no free slot was available within that time.
		Otherwise ('block' is false), put an item on the queue if a free slot
		is immediately available, else raise the Full exception ('timeout'
		is ignored in that case).
		"""
		self.not_full.acquire()
		try:
			if self.maxsize > 0:
				if not block:
					if self._qsize() == self.maxsize:
						raise Queue.Full
				elif timeout is None:
					while self._qsize() == self.maxsize:
						self.not_full.wait()
				elif timeout < 0:
					raise ValueError("'timeout' must be a positive number")
				else:
					endtime = time.time() + timeout
					while self._qsize() == self.maxsize:
						remaining = endtime - time.time()
						if remaining <= 0.0:
							raise Queue.Full
						self.not_full.wait(remaining)
			val = self._put(item)
			self.unfinished_tasks += 1
			self.not_empty.notify()
			return val
		finally:
			self.not_full.release()

	def get_element(self, interest, block=True, timeout=None):
		"""Remove and return an item from the queue.

		If optional args 'block' is true and 'timeout' is None (the default),
		block if necessary until an item is available. If 'timeout' is
		a positive number, it blocks at most 'timeout' seconds and raises
		the Empty exception if no item was available within that time.
		Otherwise ('block' is false), return an item if one is immediately
		available, else raise the Empty exception ('timeout' is ignored
		in that case).
		"""
		self.not_empty.acquire()
		try:
			if not block:
				if not self._qsize():
					raise Queue.Empty
			elif timeout is None:
				while not self._qsize():
					self.not_empty.wait()
			elif timeout < 0:
				raise ValueError("'timeout' must be a positive number")
			else:
				endtime = time.time() + timeout
				while not self._qsize():
					remaining = endtime - time.time()
					if remaining <= 0.0:
						raise Queue.Empty
					self.not_empty.wait(remaining)
			item = self._get_element(interest)
			self.not_full.notify()
			return item
		finally:
			self.not_empty.release()

class FlowController(Closure.Closure):
	queue = CCNBuffer(100)

	def __init__(self, prefix, handle):
		self.prefix = Name.Name(prefix)
		self.handle = handle

		self.cleanup_time = 15 * 60 # keep responses for 15 min
		handle.setInterestFilter(self.prefix, self)

	def put(self, co):
		co = self.queue.put(co)
		if co:
			print "Overflow; pushing: %s" % co.name
			self.handle.put(co)

	def upcall(self, kind, info):
		if kind in [Closure.UPCALL_FINAL, Closure.UPCALL_CONSUMED_INTEREST]:
			return Closure.RESULT_OK

		if kind != Closure.UPCALL_INTEREST:
			print("Got weird upcall kind: %d" % kind)
			return Closure.RESULT_ERR

#		answer_kind = info.Interest.get_aok_value()
#		print "answer_kind %d" % answer_kind
#		if (answer_kind & Interest.AOK_NEW) == 0:
#			return Closure.RESULT_OK

		print "before"
		try:
			co = self.queue.get_element(info.Interest, timeout=0.2)
		except Queue.Empty:
			return Closure.RESULT_OK

		print "after"
		print "serving %s" % co.name
		self.handle.put(co)
		self.queue.task_done()

		return Closure.RESULT_INTEREST_CONSUMED

class VersionedPull(Closure.Closure):
	def __init__(self, base_name, callback, handle=None, version=None, latest=True):
		if not handle:
			handle = CCN.CCN()

		# some constants
		self.version_marker = '\xfd'
		self.first_version_marker = self.version_marker
		self.last_version_marker = '\xfe\x00\x00\x00\x00\x00\x00'

		self.base_name = Name.Name(base_name)
		self.callback = callback
		self.handle = handle
		self.latest_version = version if version else self.first_version_marker
		self.start_with_latest = latest

	def build_interest(self, latest):
		if self.start_with_latest:
			latest=True
			self.start_with_latest = False

		excl = Interest.ExclusionFilter()
		excl.add_any()
		excl.add_name(Name.Name([self.latest_version]))
		# expected result should be between those two names
		excl.add_name(Name.Name([self.last_version_marker]))
		excl.add_any()

		interest = Interest.Interest(name=self.base_name, exclude=excl, \
			minSuffixComponents=3, maxSuffixComponents=3)
		interest.childSelector = 1 if latest else 0
		return interest

	def fetchNext(self, latest=False):
		interest = self.build_interest(latest)
		co = self.handle.get(interest.name, interest)

		if co:
			base_len = len(self.base_name)
			self.latest_version = co.name[base_len]

		return co

	def requestNext(self, latest=False):
		interest = self.build_interest(latest)
		self.handle.expressInterest(interest.name, self, interest)

	def upcall(self, kind, info):
		if kind == Closure.UPCALL_FINAL:
			return Closure.RESULT_OK

		# update version
		if kind in [Closure.UPCALL_CONTENT, Closure.UPCALL_CONTENT_UNVERIFIED]:
			base_len = len(self.base_name)
			self.latest_version = info.ContentObject.name[base_len]

		self.callback(kind, info)

		return Closure.RESULT_OK

if __name__ == '__main__':
	def make_content(name):
		global key

		co = ContentObject.ContentObject()
		co.name = Name.Name(name)
		co.signedInfo.publisherPublicKeyDigest = key.publicKeyID
		co.sign(key)
		return co

	def make_interest(name):
		return Interest.Interest(name=Name.Name(name))

	h = CCN.CCN()
	key = h.getDefaultKey()

	buf = CCNBuffer()
	co0 = make_content('/a/0')
	buf.put(co0)
	co1 = make_content('/a/1')
	buf.put(co1)
	co2 = make_content('/a/2')
	buf.put(co1)

	cr1 = buf.get_element(make_interest('/a/1'))
	assert cr1 == co1
	cr0 = buf.get_element(make_interest('/a'))
	assert cr0 == co0

