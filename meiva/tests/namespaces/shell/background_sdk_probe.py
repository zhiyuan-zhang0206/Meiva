import os

import meiva

if __name__ == "__main__":
    assert "MEIVA_HARNESS_HANDLE" not in os.environ
    print("facade-owner:", meiva.self.AGENT_ID)
