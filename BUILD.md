# How to build and update the public docker image

1. Get the git repo:
   ```
   git clone https://github.com/jgillula/amazing-marvin-autosorter.git
   cd amazing-marvin-autosorter
   ```
2. Build the docker image:
   ```
   docker build . -t amazing-marvin-autosorter
   ```
3. Tag the image for upload
   ```
   docker tag amazing-marvin-autosorter:latest flyingsaucrdude/amazing-marvin-autosorter
   docker login
   docker push flyingsaucrdude/amazing-marvin-autosorter
   ```