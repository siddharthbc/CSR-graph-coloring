import sys
import os
import numpy as np
import time
from cerebras.sdk.runtime.sdkruntimepybind import SdkRuntime, MemcpyDataType, MemcpyOrder

def main():
    dirname = "/tmp/bench_cs16"
    runner = SdkRuntime(dirname)
    
    print("Loading simulation...")
    runner.load()
    print("Running simulation...")
    runner.run()
    
    time.sleep(2)
    print("Simulation finished successfully.")
    runner.stop()

if __name__ == "__main__":
    main()
