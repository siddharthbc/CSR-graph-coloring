import sys
import os
import numpy as np
import time
from cerebras.sdk.runtime.sdkruntimepybind import SdkRuntime, MemcpyDataType, MemcpyOrder

def main():
    dirname = "/tmp/bench_multi"
    runner = SdkRuntime(dirname)
    
    # In this multi-vertex setup, we have 4 PEs and each PE handles a portion of the 16-vertex path.
    # We want to see if this configuration runs or crashes.
    
    print("Loading simulation...")
    runner.load()
    print("Running simulation...")
    runner.run()
    
    time.sleep(2)
    print("Simulation finished successfully.")
    runner.stop()

if __name__ == "__main__":
    main()
