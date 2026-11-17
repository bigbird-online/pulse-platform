import { userInfo } from 'node:os';
import { join } from 'node:path';

// Tests change HOME and child PATH; the OS account owns the one Python environment.
export const canonicalPythonPrefix = join(userInfo().homedir, '.local/share/coevolution/python-env');
export const canonicalPython = join(canonicalPythonPrefix, 'bin/python');
