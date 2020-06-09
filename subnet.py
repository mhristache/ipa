import netaddr
import logging


class SubnettingError(Exception):
    """
    To be used when a new subnet of the specified prefixlen
    cannot be created from a parent ip_set
    """
    def __init__(self, value, additional_info=None):
        self.value = value
        self.additional_info = additional_info

    def __str__(self):
        return str(self.value)


class IpNotInSubnet(Exception):
    """
    To be used when an IP is not part of a subnet
    """
    def __init__(self, ip, subnet, additional_info=None):
        self.ip = ip
        self.subnet = subnet
        self.additional_info = additional_info

    def __str__(self):
        return "IP address '{0}' is not part of subnet '{1}'"\
               .format(self.ip, self.subnet)


class IPPool(object):
    """A IPv4 or IPv6 subnet or a slice of a subnet
    """

    def __init__(self, cidr, start_ip=None, end_ip=None):
        """Create an IPPool object from an IP range (cidr).

        If start_ip and end_ip are specified, include only the IP addresses
        between them.

        If only 'start_ip' is specified, include all IP address starting from
        start_ip (inclusive) until last usable IP.

        If only 'end_ip' is specified, include all IP address starting from
        first usable IP until end_ip (inclusive).

        Note: The result is stored in self.pool which has type netaddr.IPSet

        :param cidr: an IP network
        :type cidr: str
        :param start_ip: the start IP inside the provided CIDR
        :type start_ip: str
        :param end_ip: the end IP inside the provided CIDR
        :type end_ip: str

        :raises: IpNotInSubnet

        """
        self.log = logging.getLogger(self.__class__.__name__)

        # keep track of the subnets and IP addresses assigned to other subnets
        self.reserved = netaddr.IPSet()

        # the main pool
        self.pool = None

        # a copy of the original input
        self.input = (cidr, start_ip, end_ip)

        # translate the cidr to netaddr.IPNetwork
        net = netaddr.IPNetwork(cidr)

        # save the initial net
        self.initial_net = net

        # the IP version
        self.version = net.version

        # check if an IP range (a part of a subnet) was requested
        if start_ip is not None or end_ip is not None:
            # if start_ip is missing, use the first IP in the subnet
            if start_ip is None:
                start_ip = net[0]
            else:
                # make sure the IP is part of the cidr
                if start_ip not in net:
                    raise IpNotInSubnet(start_ip, str(net))

            # if end_ip is missing, use the last usable IP in the subnet
            if end_ip is None:
                end_ip = net[-1]
            else:
                # make sure the IP is part of the cidr
                if end_ip not in net:
                    raise IpNotInSubnet(end_ip, str(net))

            # generate the IP range and store it in self.ip_range
            ip_range = netaddr.IPRange(start_ip, end_ip)

            # translate the range into an IPSet and store it in self.pool
            self.pool = netaddr.IPSet(ip_range)

        else:
            # translate the net to IPSet and store it in self.pool
            self.pool = netaddr.IPSet(net)

        self.log.debug("New IPPool created: {0}".format(self.__repr__()))

    def __repr__(self):

        if self.input[1] is not None or self.input[2] is not None:
            start = self.input[1] if self.input[1] is not None else ''
            end = self.input[2] if self.input[2] is not None else ''
            return "IPPool<'{0}-{1}'>".format(start, end)
        else:
            return "IPPool<'{0}'>".format(self.input[0])

    def allocate_subnet(self, prefixlen):
        """Generate a subnet of the specified prefixlen from a set of parent
         nets (self.pool) in the most optimal way (will try to always
        allocate the subnet from the best matching existing net in the set)

        The allocated subnet is removed from the set and saved in
        self.allocations

        :param prefixlen: the mask of the subnets to be generated
        :type prefixlen: int
        :return: the subnet allocated
        :rtype: netaddr.IPNetwork
        """
        # make the self.pool as compact as possible
        self.pool.compact()

        # find the best matching net inside the set to be used for allocation
        self.log.debug("Finding the best net in {0} to create a /{1} subnet"
                       .format(self.pool, prefixlen))

        # variable used to store the best match
        # the format is: (netaddr.IPNetwork, cost)
        # where the cost is the difference between the requested prefixlen
        # and the best_match prefixlen; ideally the cost is 0, i.e,
        # the best match subnet has the same prefixlen as the requested one
        best_match = (None, 999)

        for net in self.pool.iter_cidrs():
            # best case scenario: there is a net in the IPSet with the
            # same prefixlen as the requested one
            if net.prefixlen == prefixlen:
                best_match = (net, 0)
                # we already found the best match possible so stop searching
                break

            # if the prefixlen of the current net is lower than the requested,
            # one, this net can be used for subnetting if it's a better match
            # than the current best_match
            elif net.prefixlen < prefixlen:
                cost = prefixlen - net.prefixlen
                if cost < best_match[1]:
                    # the current net is a better match than
                    # the existing best_match
                    best_match = (net, cost)

        self.log.debug("Best match found: {0}".format(best_match))
        # error if there is no suitable subnet
        if best_match[0] is None:
            raise SubnettingError(
                "Could not allocate a /{0} subnet from {1}"
                .format(prefixlen, self.pool))

        # create and validate the subnet
        # the IPNetwork.subnet() splits the entire net into a generator of
        # subnets of the specified prefixlen
        # take the first subnet from the generator
        try:
            subnet = next(best_match[0].subnet(prefixlen))
            self.log.debug("Allocated subnet: {0}".format(subnet))

        except netaddr.AddrFormatError:
            raise SubnettingError("Invalid prefixlen: {0}".format(prefixlen))

        # save the subnet into self.reserved and remove it from self.pool
        self.reserved.add(subnet)
        self.pool.remove(subnet)
        self.log.debug("Status after allocation: pool={0}, reserved={1}"
                       .format(self.pool, self.reserved))

        return subnet

    def allocate_biggest_subnet(self):
        """Allocate the biggest available subnet in the IP pool
        :return: the biggest available subnet in the IP pool
        :rtype: netaddr.IPNetwork
        """
        # make the self.pool as compact as possible
        self.pool.compact()

        # the firs cidr returned by iter_cidrs() is the largest one
        try:
            subnet = self.pool.iter_cidrs()[0]
        except IndexError:
            raise SubnettingError(
                "Could not allocate the biggest available subnet "
                "as the IP pool is empty")
        else:
            self.reserved.add(subnet)
            return subnet


class IpRangeAllocator(object):

    def __init__(self, net, start_index=None, end_index=None):
        if not isinstance(net, netaddr.IPNetwork):
            self._net = netaddr.IPNetwork(net)
        else:
            self._net = net

        # convert the subnet into a range of usable IP addresses
        # (skip the network and broadcast IPs)
        start_idx = int(start_index) if start_index else 1
        end_idx = int(end_index) if end_index else -2
        self._range = netaddr.IPRange(self._net[start_idx], self._net[end_idx])

    def alloc(self, size, from_the_back=False):
        """Allocate an IPRange of the given size from the subnet"""
        assert size < self._range.size, \
            "Not enough addresses left to allocate the requested IP range. " \
            "Requested {}, available {}".format(size, self._range.size - 1)
        # allocate the requested range then remove those IP from the pool
        # allocate from the back of the range if that option is specified
        if from_the_back:
            sip = self._range[len(self._range) - size]
            r = netaddr.IPRange(
                self._range[len(self._range) - size], self._range.last)
            self._range = netaddr.IPRange(
                self._range.first, self._range[len(self._range) - size - 1])
        else:
            r = netaddr.IPRange(self._range.first, self._range[size - 1])
            self._range = netaddr.IPRange(self._range[size], self._range.last)
        return r
