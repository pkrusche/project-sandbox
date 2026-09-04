# TODO - outstanding items for next release

## CA-certificate injection

- Add a feature to inject a CA certificate (or multiple certificates) that will
  allow the proxy to inspect traffic / support the use of corporate proxies.

## Harden the unsupervised modes

- Currently, unsupervised sessions still have access to configs & session logs. 
  This is only really needed in the setting where we work interactively, for
  headless sessions, the environment should only contain the minimum amount of
  information and credentials to execute the task at hand.
