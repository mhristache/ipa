
Install
-------

**Note**: Python 2.7 is needed. Python 3.x is not supported.

```bash
git clone https://github.com/mhristache/ipa
cd ipa
python virtualenv venv
source venv/bin/activate
pip install -r requirements.txt
```

Use
---

To create the IP plan first time, run `ipa` with the input `yaml` file as argument and with `--first-run` flag set:

```bash
./ipa.py --first-run INPUT.yaml
```

If the input file needs to be modified (e.g. more entries added) after the IP plan was used, the current IP plan in `json` format should be provided as input to `ipa`
to make sure the currently allocated IPs are kept unchanged.

```bash
# save the original IP plan in json format
./ipa.py --first-run INPUT.yaml -o json > previous_allocation.json

# modify the input file

# create an updated IP plan that keeps the old entries unchanged
./ipa.py INPUT.yaml -p previous_allocation.json

# save a new 'previous' file
./ipa.py INPUT.yaml -p previous_allocation.json -o json > previous_allocation.json_new
mv previous_allocation.json_new previous_allocation.json
```

**Note**: currently it's only supported to add new entries to an IP plan. It's not supported to modify or delete existing entries.
```
