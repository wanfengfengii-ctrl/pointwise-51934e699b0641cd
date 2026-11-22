"""Station allowance accounting and integer split allocation for offsets."""
from collections import deque


class _Dinic:
    """Small Dinic max-flow. Capacities are Python integers of any size."""

    def __init__(self, size):
        self.size = size
        self.graph = [[] for _ in range(size)]

    def add_edge(self, source, target, capacity):
        forward = [target, capacity, len(self.graph[target])]
        backward = [source, 0, len(self.graph[source])]
        self.graph[source].append(forward)
        self.graph[target].append(backward)
        return source, len(self.graph[source]) - 1, capacity

    def residual(self, source, index):
        return self.graph[source][index][1]

    def _level(self, source, sink):
        level = [-1] * self.size
        level[source] = 0
        queue = deque([source])
        while queue:
            node = queue.popleft()
            for target, capacity, _ in self.graph[node]:
                if capacity > 0 and level[target] < 0:
                    level[target] = level[node] + 1
                    queue.append(target)
        return level if level[sink] >= 0 else None

    def _augment(self, node, sink, pushed, level, cursor):
        if node == sink:
            return pushed
        graph = self.graph[node]
        while cursor[node] < len(graph):
            edge = graph[cursor[node]]
            target, capacity, reverse = edge
            if capacity > 0 and level[target] == level[node] + 1:
                sent = self._augment(target, sink, min(pushed, capacity), level, cursor)
                if sent:
                    edge[1] -= sent
                    self.graph[target][reverse][1] += sent
                    return sent
            cursor[node] += 1
        return 0

    def max_flow(self, source, sink):
        total = 0
        while True:
            level = self._level(source, sink)
            if level is None:
                return total
            cursor = [0] * self.size
            while True:
                sent = self._augment(source, sink, float('inf'), level, cursor)
                if not sent:
                    break
                total += sent


def station_allowances(base_by_station, posted_by_station):
    """Remaining allowance per station: max(base, 0) minus posted actual offsets."""
    stations = list(base_by_station)
    for station in posted_by_station:
        if station not in base_by_station:
            stations.append(station)
    limits = {}
    for station in stations:
        start = base_by_station.get(station, 0)
        limits[station] = max(start, 0) - posted_by_station.get(station, 0)
    return limits


def balanced_split(candidates, capacities, litres):
    """Spread ``litres`` over ``candidates`` as evenly as their capacities allow.

    Returns a ``{station: positive_litres}`` mapping summing to ``litres``, or
    ``None`` when the candidates cannot carry the full quantity.  Work is
    proportional to the number of stations, not to ``litres``.
    """
    usable = []
    for station in sorted(candidates):
        capacity = capacities.get(station, 0)
        if capacity > 0:
            usable.append((station, min(capacity, litres)))
    total = sum(capacity for _, capacity in usable)
    if total < litres or not usable:
        return None
    usable.sort(key=lambda item: (item[1], item[0]))
    split = {}
    remaining = litres
    active = len(usable)
    position = 0
    while position < len(usable) and active > 0:
        station, capacity = usable[position]
        share = remaining // active
        if capacity <= share:
            split[station] = capacity
            remaining -= capacity
            active -= 1
            position += 1
            continue
        break
    if remaining == 0:
        return {station: quantity for station, quantity in split.items() if quantity > 0}
    if active == 0:
        return None
    quotient, extra = divmod(remaining, active)
    for offset, (station, capacity) in enumerate(usable[position:]):
        quantity = quotient + (1 if offset < extra else 0)
        if quantity == 0:
            continue
        if quantity > capacity or quantity <= 0:
            return None
        split[station] = quantity
    return {station: quantity for station, quantity in split.items() if quantity > 0}


def allocate(requests, capacities):
    """Return {package_id: {station: litres}} or None when not jointly feasible.

    ``requests`` is an ordered sequence of ``(package_id, litres, candidates)``;
    each request must be split entirely over its candidate stations in positive
    integer litres.  ``capacities`` maps a station to its remaining allowance.
    The returned split saturates every request, so dropping any one request
    leaves the rest jointly feasible under the residual capacities.
    """
    stations = list(capacities)
    order_count = len(requests)
    station_index = {station: index for index, station in enumerate(stations)}
    source = 0
    sink = 1 + order_count + len(stations)
    flow = _Dinic(sink + 1)
    demand = 0
    edges = {}
    for position, (package_id, litres, candidates) in enumerate(requests):
        demand += litres
        node = 1 + position
        flow.add_edge(source, node, litres)
        edges[package_id] = []
        for station in candidates:
            index = station_index.get(station)
            if index is None:
                continue
            capacity = min(litres, capacities[station])
            if capacity <= 0:
                continue
            reference = flow.add_edge(node, 1 + order_count + index, capacity)
            edges[package_id].append((station, reference))
    for station, capacity in capacities.items():
        if capacity > 0:
            flow.add_edge(1 + order_count + station_index[station], sink, capacity)
    if flow.max_flow(source, sink) != demand:
        return None
    allocation = {}
    for package_id, references in edges.items():
        split = {}
        for station, (node, index, capacity) in references:
            used = capacity - flow.residual(node, index)
            if used > 0:
                split[station] = used
        allocation[package_id] = split
    return allocation
