# Use the standard Nginx image from Docker Hub
FROM nginx

# Set default HOME in container
ENV HOME=/opt/repo

# Install python, uwsgi, supervisord and necessary python modules
RUN apt-get update && apt-get install -y python3 python3-pip && \
    /usr/bin/pip3 install uwsgi==2.0.18 flask==1.1.2 && \
    apt-get install -y git-all && \
    apt-get install -y supervisor uwsgi procps vim

# Copy app folder content to container
COPY ./app ${HOME}/app

# Copy the configuration file from the configuration directory and paste
# it inside the container to use it as Nginx's default config.
COPY ./deployment/nginx.conf /etc/nginx/nginx.conf

# Setup NGINX config
RUN mkdir -p /spool/nginx /run/pid && \
    chmod -R 777 /var/log/nginx /var/cache/nginx /etc/nginx /var/run /run /run/pid /spool/nginx && \
    chgrp -R 0 /var/log/nginx /var/cache/nginx /etc/nginx /var/run /run /run/pid /spool/nginx && \
    chmod -R g+rwX /var/log/nginx /var/cache/nginx /etc/nginx /var/run /run /run/pid /spool/nginx && \
    rm /etc/nginx/conf.d/default.conf

# Copy the base uWSGI ini file to enable default dynamic uwsgi process number
COPY ./deployment/uwsgi.ini /etc/uwsgi/apps-available/uwsgi.ini
RUN ln -s /etc/uwsgi/apps-available/uwsgi.ini /etc/uwsgi/apps-enabled/uwsgi.ini

COPY ./deployment/supervisord.conf /etc/supervisor/conf.d/supervisord.conf
RUN touch /var/log/supervisor/supervisord.log

# Expose inner port to external port
EXPOSE 5000:5000

# Setup entrypoint
COPY ./deployment/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

# Fix rights and ownership of start script
# Access to /dev/stdout
# https://github.com/moby/moby/issues/31243#issuecomment-406879017
RUN ln -sfn /usr/local/bin/docker-entrypoint.sh / && \
    chmod 777 /usr/local/bin/docker-entrypoint.sh && \
    chgrp -R 0 /usr/local/bin/docker-entrypoint.sh && \
    chown -R nginx:root /usr/local/bin/docker-entrypoint.sh

# Fix rights and ownership of system files and folders
# https://docs.openshift.com/container-platform/3.3/creating_images/guidelines.html
RUN chgrp -R 0 /var/log /var/cache /run/pid /spool/nginx /var/run /run /tmp /etc/uwsgi /etc/nginx && \
    chmod -R g+rwX /var/log /var/cache /run/pid /spool/nginx /var/run /run /tmp /etc/uwsgi /etc/nginx && \
    chown -R nginx:root ${HOME} && \
    chmod -R 777 ${HOME} /etc/passwd

# Install application specific requirements (modules)
RUN pip3 install -r ${HOME}/app/requirements.txt

# Set default workdir
WORKDIR ${HOME}

# Enter the container
ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["supervisord"]
