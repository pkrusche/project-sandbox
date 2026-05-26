  # Findings
  
  - High: README threat model overstates egress protection. README.md:139 says arbitrary exfiltration is blocked by the allowlist, and README.md:141 says C2 is blocked   
    unless allowlisted. But the firewall currently allows outbound TCP/22 to any destination in src/project_sandbox/templates/init-firewall.sh.j2:46. Either document SSH 
    as an explicit exception, or restrict port 22 to intended destinations.                                                                                               
  - Medium: README says “only /workspace is mounted” in README.md:137, but runtime containers also mount generated config, staged Claude credentials, optional Codex/     
    OpenCode/Copilot host config dirs, extra --mounts, and in worktree mode the main repo .git metadata. See src/project_sandbox/container_cli.py:43 and src/             
    project_sandbox/cli.py:417. The claim should be narrowed to “no arbitrary home directories are mounted by default” and list the intentional credential/config mounts. 
  - Medium: README’s devcontainer portability language is too broad. README.md:5, README.md:94, and README.md:165 imply Codespaces or any Docker-compatible devcontainer  
    client works generally, but the generated devcontainer depends on local generated .project-sandbox targets, an absolute host /tmp/... Claude credential mount, and    
    default NET_ADMIN/NET_RAW run args in src/project_sandbox/templates/devcontainer.json.j2:8. The new credential-refresh note helps, but remote devcontainer/Codespaces 
    usage still needs a sharper caveat.                                                                                                                               
  - Low: Project metadata is stale relative to the README and code. README.md:3 and cli.py support Claude, Codex, OpenCode, Copilot, and Bash, but pyproject.toml:8 still 
    describes only Claude Code and Codex CLI.
