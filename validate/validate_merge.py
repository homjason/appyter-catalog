import os
import sys
import json
import tempfile
import traceback
import jsonschema
import urllib.request
from subprocess import Popen, PIPE

def get_changed_appyters():
  try:
    # try to load files from stdin
    changed_files = [record['filename'] for record in json.load(sys.stdin)]
  except:
    # otherwise use git
    p = Popen(['git', 'diff', '--name-only', 'origin/master'], stdout=PIPE)
    changed_files = list(map(bytes.decode, p.stdout))
  #
  appyters = {
    file.split('/', maxsplit=3)[1]
    for file in changed_files
    if file.startswith('appyters/')
  }
  for appyter in appyters:
    print(f"{appyter}: Changed")
    assert f"appyters/{appyter}/appyter.json" in changed_files, 'Expected update to appyter.json version'
  #
  return appyters

def validate_appyter(appyter):
  print(f"{appyter}: Checking for existing of files...")
  assert os.path.isfile(os.path.join('appyters', appyter, 'README.md')), f"Missing appyters/{appyter}/README.md"
  assert os.path.isfile(os.path.join('appyters', appyter, 'appyter.json')), f"Missing appyters/{appyter}/appyter.json"
  #
  print(f"{appyter}: Validating `{appyter}/appyter.json`...")
  config = json.load(open(os.path.join('appyters', appyter, 'appyter.json'), 'r'))
  validator = jsonschema.Draft7Validator({
    '$ref': f"file:///{os.path.realpath(os.path.join(os.path.dirname(__file__), '..', 'schema', 'appyter-validator.json'))}",
  })
  errors = [error.message for error in validator.iter_errors(config)]
  assert errors == [], '\n'.join(errors)
  #
  name = config['name']
  assert name == appyter, f"The directory should be named like `name`"
  #
  nbfile = config['appyter']['file']
  #
  print(f"{appyter}: Preparing docker to run `{nbfile}`...")
  assert os.path.isfile(os.path.join('appyters', appyter, nbfile)), f"Missing appyters/{appyter}/{nbfile}"
  try:
    json.load(open(os.path.join('appyters', appyter, nbfile), 'r'))
  except Exception as e:
    print(f"{nbfile} is not valid json")
    traceback.print_exc()
  #
  assert not os.path.isfile(os.path.join('appyters', appyter, 'Dockerfile')), 'Custom Dockerfiles are no longer supported'
  print(f"{appyter}: Creating Dockerfile...")
  import sys; sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
  from compose.build_dockerfile import prepare_appyter
  with open(os.path.join('appyters', appyter, 'Dockerfile'), 'w') as fw:
    print(prepare_appyter(os.path.join('appyters', appyter), config), file=fw)
  #
  print(f"{appyter}: Building Dockerfile...")
  with Popen([
    'docker', 'build',
    '-t', f"maayanlab/appyters-{config['name'].lower()}:{config['version']}",
    '.',
  ], cwd=os.path.join('appyters', appyter), stdout=PIPE) as p:
    for line in p.stdout:
      print(f"{appyter}: `docker build .`: {line}")
    assert p.wait() == 0, '`docker build .` command failed'
  #
  print(f"{appyter}: Inspecting appyter...")
  with Popen([
    'docker', 'run',
    f"maayanlab/appyters-{config['name'].lower()}:{config['version']}",
    'appyter', 'nbinspect',
    nbfile,
  ], stdout=PIPE) as p:
    nbinspect_output = p.stdout.read().decode()
    print(f"{appyter}: `appyter nbinspect {nbfile}`: {nbinspect_output})")
    assert p.wait() == 0, f"`appyter nbinspect {nbfile}` command failed"
  #
  inspect = json.loads(nbinspect_output)
  field_args = {
    field['args']['name']: field['args']
    for field in inspect
  }
  assert len(field_args) == len(inspect), "Some of your fields weren't captured, there might be duplicate `name`s"
  #
  print(f"{appyter}: Preparing defaults...")
  tmp_directory = os.path.realpath('.tmp')
  os.makedirs(tmp_directory, exist_ok=True)
  default_args = {
    field_name: field.get('default')
    for field_name, field in field_args.items()
  }
  file_fields = {
    field['args']['name']
    for field in inspect
    if field['field'] == 'FileField'
  }
  for file_field in file_fields:
    field_examples = field_args[file_field].get('examples', {})
    default_file = default_args[file_field]
    if default_file:
      if default_file in field_examples:
        print(f"{appyter}: Downloading example file {default_file} from {field_examples[default_file]}...")
        urllib.request.urlretrieve(field_examples[default_file], filename=os.path.join(tmp_directory, default_file))
      else:
        print(f"{appyter}: WARNING, default file isn't in examples, we won't know how to get it if it isn't available in the image")
    else:
      print(f"{appyter}: WARNING, no default file is provided")
  #
  print(f"{appyter}: Constructing default notebook from appyter...")
  with Popen([
    'docker', 'run',
    '-v', f"{tmp_directory}:/data",
    "-i", f"maayanlab/appyters-{config['name'].lower()}:{config['version']}",
    'appyter', 'nbconstruct',
    f"--output=/data/{nbfile}",
    nbfile,
  ], stdin=PIPE, stdout=PIPE) as p:
    print(f"{appyter}: `appyter nbconstruct {nbfile}` < {default_args}")
    stdout, _ = p.communicate(json.dumps(default_args).encode())
    for line in stdout:
      print(f"{appyter}: `appyter nbconstruct {nbfile}`: {line}")
    assert p.wait() == 0, f"`appyter nbconstruct {nbfile}` command failed"
    assert os.path.exists(os.path.join(tmp_directory, config['appyter']['file'])), 'nbconstruct output was not created'
  #
  print(f"{appyter}: Executing default notebook with appyter...")
  with Popen([
    'docker', 'run',
    '-v', f"{tmp_directory}:/data",
    f"maayanlab/appyters-{config['name'].lower()}:{config['version']}",
    'appyter', 'nbexecute',
    f"--cwd=/data",
    f"/data/{nbfile}",
  ], stdout=PIPE) as p:
    for msg in map(json.loads, p.stdout):
      assert msg['type'] != 'error', f"{appyter}: error {msg.get('data')}"
      print(f"{appyter}: `appyter nbexecute {nbfile}`: {json.dumps(msg)}")
    assert p.wait() == 0, f"`appyter nbexecute {nbfile}` command failed"
  #
  print(f"{appyter}: Success!")

if __name__ == '__main__':
  valid = True
  for appyter in get_changed_appyters():
    if not os.path.exists(os.path.join('appyters', appyter)):
      print(f"{appyter}: Directory no longer exists, ignoring")
      continue
    elif not os.path.isdir(os.path.join('appyters', appyter)):
      print(f"{appyter}: Is not a directory, ignoring")
      continue
    try:
      validate_appyter(appyter)
    except Exception as e:
      print(f"{appyter}: ERROR {str(e)}")
      traceback.print_exc()
      valid = False
  if valid:
    sys.exit(0)
  else:
    sys.exit(1)
