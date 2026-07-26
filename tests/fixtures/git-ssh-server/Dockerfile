# A minimal git-over-SSH server, standing in for a forge.
#
# It exists so the SSH push path can be proven end to end without a real
# forge account: a bare repo, key-only auth, and a host key we can pin.
FROM alpine:3.20

RUN apk add --no-cache openssh-server git

# Key-only auth. No password path exists at all, so a test that "passes"
# cannot have passed by some other means.
RUN adduser -D -s /bin/sh git \
 && sed -i 's/^git:!:/git:*:/' /etc/shadow \
 && mkdir -p /home/git/.ssh

COPY authorized_keys /home/git/.ssh/authorized_keys

RUN chown -R git:git /home/git/.ssh \
 && chmod 700 /home/git/.ssh \
 && chmod 600 /home/git/.ssh/authorized_keys

# Host key generated at build time; the test reads the PUBLIC half straight
# out of the image to build known_hosts, so nothing is trusted on first use.
RUN ssh-keygen -A

RUN printf '%s\n' \
      'PasswordAuthentication no' \
      'PermitRootLogin no' \
      'PubkeyAuthentication yes' \
      'AllowUsers git' \
      > /etc/ssh/sshd_config.d/agentbox.conf

# A bare repo with one commit, so `clone` has something to show and a later
# re-clone can prove the pushed commit actually landed.
RUN git init --bare -b main /srv/git/agentbox-push-test.git \
 && git config --global user.email seed@example.invalid \
 && git config --global user.name seed \
 && git config --global init.defaultBranch main \
 && git clone /srv/git/agentbox-push-test.git /tmp/seed \
 && echo "seed" > /tmp/seed/README.md \
 && git -C /tmp/seed add README.md \
 && git -C /tmp/seed commit -q -m "seed commit" \
 && git -C /tmp/seed push -q origin main \
 && rm -rf /tmp/seed \
 && chown -R git:git /srv/git

EXPOSE 22
CMD ["/usr/sbin/sshd", "-D", "-e"]
