# Raspberry Pi Backup
- a simple cli,tui, x-windows app based backup program for raspberry pi
- a server hosts the backup repository
- clients connect to the server and upload their files
- server shows the backup jobs, last run, status (success/failure), retention period
- very easy to setup
- backups are optionally encrypted
- easy to add new raspberry pis
- easy to restore to a new pi / sd card
- can restore hostname, installed libraries etc
- doesn't block network traffic sending large files across (investigate BITS for slower, background file transfer)
- entirely stand-alone; doens't need any other software to backup files (no dependency on any other backup software); is rsync available on every pi - this could be the mechanism to backup
- compresses file transfers for quick transfer to storage medium (which is managed by the server)
- investigate backup options - tar/zip/Z/arc etc for compression otptions, evaluate the best solution for this


