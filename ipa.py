#!/usr/bin/env python

from collections import OrderedDict
import json
import argparse
import sys
from ruamel.yaml import YAML
from subnet import *
import copy


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

    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('-p',
                       dest="previous_alloc",
                       metavar="FILE.json",
                       help='the result of a previous run/allocation, '
                            'in json format.')

    group.add_argument('--first-run',
                       dest="is_first_run",
                       action="store_true",
                       help="use this option when it's the first allocation "
                            "done with the given input file (i.e. there are "
                            "no previous ip allocations that have to be "
                            "preserved)")

    parser.add_argument('--version', action='version', version='0.2')

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
        return json.dumps(deobjectify(res), indent=2)
    elif args.output_format == 'yaml-anchors':
        return to_yaml_anchors(res)
    elif args.output_format == 'human':
        return to_human(res)


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


class VlanPool(object):

    def __init__(self, first, last):
        self.first = first
        self.last = last
        self.pool = iter(range(first, last))

    def alloc(self):
        return next(self.pool)

    def unused(self):
        u = list(self.pool)
        return u[0], u[-1] + 1


def convert_vlans(d):
    # TODO: add some validation
    return {k: VlanPool(v['start'], v['end'])
            for k, v in d['vlan_pool'].items()}


def alloc_ips(d, p):
    """Allocate IPs
    :param d: the content of the input file as dict
    :param p: the result of a previous allocation as dict
    :return: dict
    """
    tmp = {}

    vp = convert_vlans(d)
    ipp = convert_subnets(d)
    ipr = {}  # keep track of the IP ranges per subnet

    def run_for(input_):

        deferred = OrderedDict()

        for k, s in input_.items():
            v = d['ipam'][k[0]]

            # if 'size' is specified, a new range should be allocated
            # from a subnet created before so we defer this allocation
            # until after all normal subnets are allocated
            if 'size' in s:
                # find the key to the parent subnet
                # from where the range is supposed to be allocated from
                parent_str = v['ip_range'][s['label']]
                parent = parent_str.split('.')
                assert len(parent) == 2,\
                    "'{}' does not have the expected format (<node>.<entry> " \
                    "or .<entry>)".format(parent_str)
                deferred[k] = (s, parent[0], parent[1])
                continue

            subnet_name = v['subnet'][s['label']]
            ip_pool = ipp[subnet_name]

            vlan_pool_name = v.get('vlan_pool', {}).get(s['label'])
            vlan_pool = vp.get(vlan_pool_name)

            # allocate a vlan is there is a vlan pool defined for the label
            vid = vlan_pool.alloc() if vlan_pool is not None else None

            # allocate a new subnet if prefixlen is specified
            if 'prefixlen' in s:
                kind = 'subnet'

                net = ip_pool.allocate_subnet(s['prefixlen'])

                # skip the first and the last IP  (network and broadcast)
                # if there are at least 4 usable IPs in the subnet
                eidx = -2 if net.size >= 4 else -1
                sidx = 1 if net.size >= 4 else 0
                ip_range = netaddr.IPRange(net[sidx], net[eidx])

            else:
                raise NotImplementedError

            # reserve the last usable IP for the gateway
            # if the net is big enough for that
            gw_ip = net[-2] if net.size >= 4 else None

            s['metadata'].update({'type': kind, 'label': s['label']})

            tmp[k] = {
                'vlan': vid,
                'ip_range': ip_range,
                'gateway': gw_ip,
                'cidr': net,
                'prefixlen': net.prefixlen,
                'netmask': net.netmask,
                'properties': s.get('properties', {}),
                'metadata': s['metadata'],
            }

        # handled deferred allocations
        for k, v in deferred.items():
            s, node_k, entry_k = v

            # use the current node if there is no key for the parent node
            node_k = v[1] or k[0]

            net = tmp[(node_k, entry_k)]['cidr']

            # make sure the last IP is not used
            # so that it can be used for the gateway
            # start the ip range from -3 as -2 is the last usable ip
            eidx = -3 if net.size >= 4 else -2
            ip_range = ipr.setdefault(
                (node_k, entry_k), IpRangeAllocator(net, end_index=eidx))

            # the sign of the size parameter is used to indicate
            # if the alloc should be done from the back
            if s['size'] < 0:
                size = s['size'] * -1
                from_the_back = True
            else:
                size = s['size']
                from_the_back = False

            ip_range = ip_range.alloc(size, from_the_back)

            # reserve the last usable IP for the gateway
            # if the net is big enough for that
            gw_ip = net[-2] if net.size >= 4 else None

            s['metadata'].update({'type': 'ip_range',
                                  'parent': (node_k, entry_k),
                                  'label': s['label']})

            tmp[k] = {
                'vlan': None,
                'ip_range': ip_range,
                'gateway': gw_ip,
                'cidr': net,
                'prefixlen': net.prefixlen,
                'netmask': net.netmask,
                'properties': s.get('properties', {}),
                'metadata': s['metadata'],
            }

    old, new = filter_entries(d, p)

    # process the new entries last to avoid new entries
    # taking over IPs for old entries
    run_for(old)
    run_for(new)

    # create the final data structure
    res = OrderedDict()

    for k, v in d['ipam'].items():
        res[k] = {'properties': v.get('properties', {}), 'ipa': OrderedDict()}
        for s in v['schema']:
            res[k]['ipa'][s['name']] = tmp[(k, s['name'])]

    r = {
        'ipam': res,
        'ip_pool': ipp,
        'vlan_pool': vp,
    }
    # pass along any global properties
    if d.get('properties'):
        r['properties'] = d['properties']
    return r


def filter_entries(d, p):
    """Separate the new entries from the old/previously created ones"""
    new = OrderedDict()
    old = OrderedDict()
    for k, v in d['ipam'].items():
        for s in v['schema']:
            # check if there is a previous allocation for the current entry
            pv = p.get('ipam', {}).get(k, {}).get('ipa', {}).get(s['name'])
            if pv is None:
                # defer IP allocation for the new entries to the end
                new[(k, s['name'])] = copy.deepcopy(s)
            else:
                old[(k, s['name'])] = copy.deepcopy(s)
                # propagate the metadata
                old[(k, s['name'])]['metadata'] = pv['metadata']

    # find the last used id then allocate ids for the new entries
    last_id = max([x['metadata']['id'] for x in old.values()] or [0])
    for k, v in new.items():
        # each entry gets an id in consecutive order of definition
        # the id is used to keep track of entries which are added later
        # (not included in the first version of the input file)
        new[k] = copy.deepcopy(v)
        new[k].setdefault('metadata', {})['id'] = last_id + 1
        last_id += 1

    # sort the old values based on the id
    # as allocation is done in the order inside the dict
    old = OrderedDict(
        sorted(old.items(), key=lambda item: item[1]['metadata']['id']))

    return old, new


def ip_range_to_dict(r):
    """Convert an IPRange to a dict"""
    return {
        'start': str(r[0]),
        'end': str(r[-1]),
        'str': str(r),
        'size': r.size,
    }


def ip_pool_to_dict(ipp):
    """Convert an IPPool object to a dict"""
    return {
        'input': ipp.input[0],
        'unused': [str(x) for x in ipp.pool.iter_cidrs()],
    }


def dict_to_ip_pool(d):
    """The reverse operation to ip_pool_to_dict()"""
    ipp = IPPool(d['input'])
    ipp.pool = netaddr.IPSet(d['unused'])
    return ipp


def vlan_pool_to_dict(vp):
    """Convert a VlanPool to a dict"""
    return {
        'input': (vp.first, vp.last),
        'unused': vp.unused()
    }


def dict_to_vlan_pool(d):
    """The reverse operation to vlan_pool_to_dict()"""
    vp = VlanPool(*d['input'])
    vp.pool = iter(range(*d['unused']))
    return vp


def deobjectify(d):
    """Remove the objects from the return dict"""
    for entry in d['ipam'].values():
        for v in entry['ipa'].values():
            v['ip_range'] = ip_range_to_dict(v['ip_range'])
            v['netmask'] = str(v['netmask'])
            v['cidr'] = str(v['cidr'])
            v['gateway'] = str(v['gateway']) if v['gateway'] else None

    for k, v in d['ip_pool'].items():
        d['ip_pool'][k] = ip_pool_to_dict(v)

    for k, v in d['vlan_pool'].items():
        d['vlan_pool'][k] = vlan_pool_to_dict(v)

    return d


def objectify(d):
    """Convert strings to netaddr objects where applicable
    Note: this is the reverse operation of deobjectify()
    """
    for entry in d['ipam'].values():
        for v in entry['ipa'].values():
            v['ip_range'] = netaddr.IPRange(v['ip_range']['start'],
                                            v['ip_range']['end'])
            v['netmask'] = netaddr.IPAddress(v['netmask'])
            v['cidr'] = netaddr.IPNetwork(v['cidr'])
            if v['gateway']:
                v['gateway'] = netaddr.IPAddress(v['gateway'])

    for k, v in d['ip_pool'].items():
        d[k] = dict_to_ip_pool(v)

    for k, v in d['vlan_pool'].items():
        d[k] = dict_to_vlan_pool(v)

    return d


def to_yaml_anchors(d):
    """Convert the response to an yaml anchor string that can be used in
    other yaml files, e.g. in j2i templates"""
    deobjectify(d)
    res = []

    def create_anchor(k, v, s='- &'):
        if isinstance(v, (basestring, int, list)):
            s += '{} {}'.format(k, v)
            res.append(s)
        elif isinstance(v, dict):
            if v.get('properties', {}).get('reserved', False):
                return
            new_s = '{}{}_'.format(s, k)
            for k1, v1 in v.items():
                create_anchor(k1, v1, new_s)

    for k_, v_ in d['ipam'].items():
        create_anchor(k_, v_)
    return "\n".join(['ipam:'] + sorted(res))


def to_human(d):
    """Convert the response to a human readable format"""
    deobjectify(d)
    d = d['ipam']

    r = []

    # calculate the longest key names and use the info to align the output
    l_nf = max(
        [len(x) for x in d.keys()] +
        [len("NF")]
    )
    l_net = max(
        [len(x) for v in d for x in d[v]['ipa'].keys()] +
        [len("NET")]
    )
    l_ip = max(
        [len(x['cidr']) for v in d for x in d[v]['ipa'].values()] +
        [len("CIDR")]
    )
    l_vlan = max(
        [len(str(x['vlan'])) for v in d for x in d[v]['ipa'].values()] +
        [len("VLAN")]
    )
    l_ipr = max(
        [len(x['ip_range']['str']) for v in d for x in d[v]['ipa'].values()] +
        [len("IP_RANGE")]
    )
    l_gw = max(
        [len(x['gateway'] or '-')for v in d for x in d[v]['ipa'].values()] +
        [len("GW_IP")]
    )
    l_desc = max(
        [len(x.get('properties', {}).get('desc') or '-')
         for v in d for x in d[v]['ipa'].values()] +
        [len("DESCRIPTION")]
    )

    def add_entry(c1, c2, c3, c4, c5, c6, c7):
        template = "{}  {}  {}  {}  {}  {}  {}"
        r.append(template.format(c1.ljust(l_nf),
                                 c2.ljust(l_net),
                                 c3.ljust(l_ip),
                                 c4.ljust(l_ipr),
                                 c5.ljust(l_gw),
                                 c6.ljust(l_vlan),
                                 c7.ljust(l_desc)))
    # add the title
    add_entry("NF", "NET", "CIDR", "IP_RANGE", "GW_IP", "VLAN", "DESCRIPTION")
    r.append("-" * (l_nf + l_net + l_ip + l_vlan + l_ipr + l_gw + l_desc + 10))

    for k, v in d.items():
        for k1, v1 in v['ipa'].items():
            # do not print the reserved IPs
            if v1.get('properties', {}).get('reserved', False):
                continue
            add_entry(k,
                      k1,
                      v1['cidr'],
                      v1['ip_range']['str'],
                      v1['gateway'] or '-',
                      str(v1['vlan'] or '-'),
                      v1.get('properties', {}).get('desc') or '-')

    return "\n".join(r)


if __name__ == "__main__":
    print(main(sys.argv[1:]))
