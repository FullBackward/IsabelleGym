import os
import sys
from typing import List, Optional
from .operation import Success, Failure
from pathlib import Path

DIR_ROOT = Path(__file__).parent.parent.parent.resolve()
#print(DIR_ROOT)
thys_pool_dir = DIR_ROOT / "thys"
filepath = thys_pool_dir / "init.thy"
#print(filepath)
thys_pool = ["Analysis_init", "Complex_Main_init", "Pure_init"]

class ThyInit:
    async def __init__(self, init_filepath = filepath) -> Optional[None]:
        self.filepath = filepath
        try:
            with open(self.filepath, 'r') as f:
                self.template = f.read()
        except FileNotFoundError:
            raise RuntimeError(f"init.thy file not found at {self.filepath}")
        except IOError as e:
            #print(f"Error reading {self.filepath}: {e}", file=sys.stderr)
            raise RuntimeError(f"Error reading init.thy file at {self.filepath}")
        self.created_files = []
    
    def format_thys(self, thys: List[str]) -> str:
        """Creates an Isabelle theory file that imports the given theories."""
        if not thys:
            return ""
        quoted_theories = [f'"{theory}"' for theory in thys]
        return " ".join(quoted_theories)
    
    async def generate_thy_file(self, thys: List[str], filename: str) -> str:
        """Generates the content of an Isabelle theory file that imports the given theories."""
        imports_str = self.format_thys(thys)
        content = self.template.replace("[thys]", imports_str)
        content = content.replace("[filename]", filename)
        return content
    
    async def gen_file(self, filename:str, thys: List[str]) -> Success | Failure:
        """Writes the generated theory file to the specified output path."""
        for thy in thys:
            if thy in thys_pool:
                return Success(msg="No generation needed for pool theories.", data = thy)
        output_filepath = os.path.join(thys_pool_dir, filename + ".thy")
        content = self.generate_thy_file(thys, filename)
        try:
            with open(output_filepath, 'w') as f:
                f.write(content)
        except IOError as e:
            return Failure(err=f"Failed to write to {output_filepath}: {e}")
        self.created_files.append(filename)
        return Success(msg=f"Generated {output_filepath}.", data = filename)
    
    def cleanup(self, filename:str) -> Success | Failure:
        """Cleans up the generated theory file."""
        if filename not in self.created_files:
            return Failure(err="No cleanup needed for untracked files.")
        if filename in thys_pool:
            return Success(msg="No cleanup needed for pool theories.")
        output_filepath = os.path.join(thys_pool_dir, filename + ".thy")
        try:
            if os.path.exists(output_filepath):
                os.remove(output_filepath)
                return Success(msg=f"Deleted {output_filepath}.")
        except IOError as e:
            print(f"Error deleting {output_filepath}: {e}", file=sys.stderr)
            return Failure(err=f"Failed to delete {output_filepath}.")


#thyinit = ThyInit()
#if thyinit is None:
#    raise RuntimeError("Failed to initialize ThyInit: init.thy file not found.")

#print(thyinit.gen_file("test_session", ["Analysis", "Complex_Main"]).data)