# How to build and update the public docker image

1. Get the git repo:
   ```
   git clone https://github.com/jgillula/amazing-marvin-autosorter.git
   cd amazing-marvin-autosorter
   ```
2. Build the docker image:
   ```
   docker build . -t flyingsaucrdude/amazing-marvin-autosorter
   ```
3. Upload the docker image
   ```
   docker login
   docker push flyingsaucrdude/amazing-marvin-autosorter
   ```