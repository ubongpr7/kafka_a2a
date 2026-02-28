FROM redis:7.2-alpine

COPY redis.conf /usr/local/etc/redis/redis.conf
COPY entrypoint.sh /usr/local/bin/entrypoint.sh

RUN chmod +x /usr/local/bin/entrypoint.sh

EXPOSE 6379

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]

