import json
import copy

f = open('/home/ec2-user/_work/dd-trace-py/results/python.sarif',)

data = json.load(f)
temp = copy.deepcopy(data)

# List of paths to ignore
ignore_path_list = ["tests/tracer/"]

# Delete the finding to be ignored
def delete_node(count):
    del temp["runs"][0]["results"][count]

# Check if the given path matches with any of the configured ignored paths
def check_path(given_path):
    for ignore in ignore_path_list:
        if given_path.startswith(ignore):
            return True

"""
Iterating over the generated Sarif and checking if any of the artifactLocation URI
matches with the ignored paths.
"""
count =  0
for result in data["runs"][0]["results"]:
    # print(result["locations"][0]["physicalLocation"]["artifactLocation"]["uri"])
    if check_path(result["locations"][0]["physicalLocation"]["artifactLocation"]["uri"]):
        delete_node(count)
        count -= 1
    count += 1

f.close()    

# ---------------------------------------------------------------------------------------#

"""
Dumps the new SARIF after removing all unimportant redundant alerts
"""
with open('/home/ec2-user/_work/dd-trace-py/results/python-new.sarif', 'w') as data_file:
    data = json.dump(temp, data_file)    

