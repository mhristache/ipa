#!/usr/bin/env python

from collections import OrderedDict
import json
import argparse
import sys
from ruamel.yaml import YAML
from subnet import *


def main(input_args):
    parser = argparse.ArgumentParser(description='Basic IPAM tool')
    parser.add_argument(dest="input_file",
                        help='the input file in yaml format')

    output_formats = ['human', 'json', 'yaml-anchors']
    parser.add_argument('-o',
                        dest="output_format",
                        default="human",
                        choices=output_formats,
                        help='the format of the output.'
                             'Supported options: {}. Default: human'
                             .format(", ".join(output_formats)))

    parser.add_argument('-p',
                        dest="previous_alloc",
                        metavar="FILE.json",
                        help='the result of a previous run/allocation, '
                             'in json format.')

    parser.add_argument('--version', action='version', version='0.1')

    args = parser.parse_args(input_args)

    yaml = YAML()
    with open(args.input_file) as f:
        input_dict = yaml.load(f)

    palloc = {}
    if args.previous_alloc:
        with open(args.previous_alloc) as f:
            palloc = objectify(json.load(f))

    res = alloc_ips(input_dict, palloc)

    if args.output_format == 'json':
        print(json.dumps(deobjectify(res), indent=2))
    elif args.output_format == 'yaml-anchors':
        print(to_yaml_anchors(res))
    elif args.output_format == 'human':
        print(to_human(res))


def convert_subnets(d):
    # convert the input subnets into IPPools
    # input subnets can also be created dynamically from another subnet
    # in that case a 'prefixlen' and a 'from' subnet should be provided
    acc = {}
    pools = netaddr.IPSet()

    def convert_subnet(k):
        v = d['subnet'][k]
        if 'cidr' in v:
            ipp = IPPool(v['cidr'])
            assert not pools.intersection(ipp.pool),\
                "Subnet {} is overlapping with previous subnets"\
                .format(v['cidr'])
            pools.update(ipp.pool)
            acc[k] = ipp

        elif 'from' in v:
            parent = acc.get(v['from'])
            if parent is None:
                convert_subnet(v['from'])
            else:
                subnet = parent.allocate_subnet(v['prefixlen'])
                acc[k] = IPPool(str(subnet))

    for k_ in d['subnet']:
        convert_subnet(k_)

    return acc


def convert_vlans(d):
    # TODO: add some validation
    return {k: range(v['start'], v['end']) for k, v in d['vlan_pool'].items()}


def alloc_ips(d, p):
    """Allocate IPs
    :param d: the content of the input file as dict
    :param p: the result of a previous allocation as dict
    :return: dict
    """

    res = OrderedDict()

    vp = convert_vlans(d)
    ipp = convert_subnets(d)

    ipr = {}  # keep track of the IP ranges per subnet

    def run_for(input_):

        deferred = OrderedDict()

        for k, s in input_.items():

            v = d['ipam'][k[0]]
            entry = res.setdefault(k[0], {'metadata': v.get('metadata', {}),
                                          'ipam': OrderedDict()})

            # if 'from' is specified, a new subnet should be allocated
            # from a subnet created before so we defer this allocation
            # until after all normal subnets are allocated
            if 'from' in s:
                deferred[k] = s
                continue

            subnet_name = v['subnet'][s['label']]
            ip_pool = ipp[subnet_name]

            vlan_pool_name = v.get('vlan_pool', {}).get(s['label'])
            vlan_pool = vp.get(vlan_pool_name)

            # allocate a vlan is there is a vlan pool defined for the label
            vid = vlan_pool.pop(0) if vlan_pool is not None else None

            # allocate a new subnet if prefixlen is specified
            if 'prefixlen' in s:
                kind = 'subnet'

                net = ip_pool.allocate_subnet(s['prefixlen'])

                # skip the first and the last IP  (network and broadcast)
                # if there are at least 4 usable IPs in the subnet
                eidx = -2 if net.size >= 4 else -1
                sidx = 1 if net.size >= 4 else 0
                ip_range = netaddr.IPRange(net[sidx], net[eidx])

            # allocate a range/part of the net if size is specified
            elif 'size' in s:
                kind = 'ip_range_global'
                # TODO: this is more a hack to avoid issues with empty pool
                # as the pool is allocated when the parent is created
                net = netaddr.IPNetwork(ip_pool.input[0])

                # make sure the last IP is not used
                # so that it can be used for the gateway
                # start the ip range from -3 as -2 is the last usable ip
                eidx = -3 if net.size >= 4 else -2

                ip_range = ipr.setdefault(
                    subnet_name, IpRangeAllocator(net, end_index=eidx))
                ip_range = ip_range.alloc(s['size'])

            else:
                raise NotImplementedError

            # reserve the last usable IP for the gateway
            # if the net is big enough for that
            gw_ip = net[-2] if net.size >= 4 else None

            entry['ipam'][s['name']] = {
                'vlan': vid,
                'metadata': s.get('metadata', {}),
                'label': s['label'],
                'ip_range': ip_range,
                'gateway': gw_ip,
                'cidr': net,
                'prefixlen': net.prefixlen,
                'netmask': net.netmask,
                'kind': kind
            }

        # handled deferred allocations
        d_ipr = {}
        for k, s in deferred.items():
            kind = "ip_range_local"

            entry = res[k[0]]
            net = entry['ipam'][s['from']]['cidr']

            # make sure the last IP is not used
            # so that it can be used for the gateway
            # start the ip range from -3 as -2 is the last usable ip
            eidx = -3 if net.size >= 4 else -2
            ip_range = d_ipr.setdefault(
                (k[0], s['from']), IpRangeAllocator(net, end_index=eidx))
            ip_range = ip_range.alloc(s['size'])

            # reserve the last usable IP for the gateway
            # if the net is big enough for that
            gw_ip = net[-2] if net.size >= 4 else None

            entry['ipam'][s['name']] = {
                'vlan': None,
                'metadata': s.get('metadata', {}),
                'label': None,
                'ip_range': ip_range,
                'gateway': gw_ip,
                'cidr': net,
                'prefixlen': net.prefixlen,
                'netmask': net.netmask,
                'kind': kind
            }

    old, new = filter_entries(d, p)

    # process the new entries last to avoid new entries
    # taking over IPs for old entries
    run_for(old)
    run_for(new)

    return res


def filter_entries(d, p):
    """Separate the new entries from the old/previously created ones"""
    new = OrderedDict()
    old = OrderedDict()
    for k, v in d['ipam'].items():
        for s in v['schema']:
            # check if there is a previous allocation for the current entry
            pv = p.get(k, {}).get('ipam', {}).get(s['name'])
            if pv is None:
                # defer IP allocation for the new entries to the end
                new[(k, s['name'])] = s
            else:
                old[(k, s['name'])] = s
    return old, new


def ip_range_to_dict(r):
    """Convert an IPRange to a dict"""
    return {
        'start': str(r[0]),
        'end': str(r[-1]),
        'str': str(r),
        'size': len(r),
    }


def deobjectify(d):
    """Remove the objects from the return dict"""
    for entry in d.values():
        for v in entry['ipam'].values():
            v['ip_range'] = ip_range_to_dict(v['ip_range'])
            v['netmask'] = str(v['netmask'])
            v['cidr'] = str(v['cidr'])
            v['gateway'] = str(v['gateway']) if v['gateway'] else None
    return d


def objectify(d):
    """Convert strings to netaddr objects where applicable
    Note: this is the reverse operation of deobjectify()
    """
    for entry in d.values():
        for v in entry['ipam'].values():
            v['ip_range'] = netaddr.IPRange(v['ip_range']['start'],
                                            v['ip_range']['end'])
            v['netmask'] = netaddr.IPAddress(v['netmask'])
            v['cidr'] = netaddr.IPNetwork(v['cidr'])
            if v['gateway']:
                v['gateway'] = netaddr.IPAddress(v['gateway'])
    return d


def to_yaml_anchors(d):
    """Convert the response to an yaml anchor string that can be used in
    other yaml files, e.g. in j2i templates"""
    deobjectify(d)

    res = ["ipam:"]

    def create_anchor(k, v, s='- &'):
        if isinstance(v, (basestring, int, list)):
            s += '{} {}'.format(k, v)
            res.append(s)
        elif isinstance(v, dict):
            if v.get('metadata', {}).get('reserved', False):
                return
            new_s = '{}{}_'.format(s, k)
            for k1, v1 in v.items():
                create_anchor(k1, v1, new_s)

    for k_, v_ in d.items():
        create_anchor(k_, v_)

    return "\n".join(res)


def to_human(d):
    """Convert the response to a human readable format"""
    deobjectify(d)

    r = []

    # calculate the longest key names and use the info to align the output
    l_nf = max(
        [len(x) for x in d.keys()] +
        [len("NF")]
    )
    l_net = max(
        [len(x) for v in d for x in d[v]['ipam'].keys()] +
        [len("NET")]
    )
    l_ip = max(
        [len(x['cidr']) for v in d for x in d[v]['ipam'].values()] +
        [len("CIDR")]
    )
    l_vlan = max(
        [len(str(x['vlan'])) for v in d for x in d[v]['ipam'].values()] +
        [len("VLAN")]
    )
    l_ipr = max(
        [len(x['ip_range']['str']) for v in d for x in d[v]['ipam'].values()] +
        [len("IP_RANGE")]
    )
    l_gw = max(
        [len(x['gateway'] or '-')for v in d for x in d[v]['ipam'].values()] +
        [len("GW_IP")]
    )

    def add_entry(c1, c2, c3, c4, c5, c6):
        template = "{}  {}  {}  {}  {} {}"
        r.append(template.format(c1.ljust(l_nf),
                                 c2.ljust(l_net),
                                 c3.ljust(l_ip),
                                 c4.ljust(l_ipr),
                                 c5.ljust(l_gw),
                                 c6.ljust(l_vlan)))
    # add the title
    add_entry("NF", "NET", "CIDR", "IP_RANGE", "GW_IP", "VLAN")
    r.append("-" * (l_nf + l_net + l_ip + l_vlan + l_ipr + l_gw + 10))

    for k, v in d.items():
        for k1, v1 in v['ipam'].items():
            # do not print the reserved IPs
            if v1.get('metadata', {}).get('reserved', False):
                continue
            add_entry(k,
                      k1,
                      v1['cidr'],
                      v1['ip_range']['str'],
                      v1['gateway'] or '-',
                      str(v1['vlan'] or '-'))

    return "\n".join(r)


if __name__ == "__main__":
    main(sys.argv[1:])
