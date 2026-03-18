FROM python:3.12-slim


WORKDIR /app


RUN apt-get update && apt-get install -y \
# try fix with bash
    bash \
    openjdk-21-jdk-headless \
    wget \
    curl \
    git \
    tar \
    gzip \
    htop \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*


ENV JAVA_HOME=/usr/lib/jvm/java-21-openjdk-amd64
ENV PATH=$JAVA_HOME/bin:$PATH


COPY . /app/


RUN mkdir -p /opt \
 && cd /opt \
 && wget https://isabelle.in.tum.de/dist/Isabelle2025-2_linux.tar.gz \
 && tar -xf Isabelle2025-2_linux.tar.gz \
 && mv Isabelle2025-2 isabelle \
 && rm -f Isabelle2025-2_linux.tar.gz

ENV ISABELLE_HOME=/opt/isabelle
ENV PATH=$ISABELLE_HOME/bin:$PATH

# Global Python deps (no venv)
RUN python -m pip install --upgrade pip \
 && python -m pip install -r /app/requirement.txt

# Windows 
RUN find repl -type f \( -name "*.sh" -o -name "init" -o -name "gradlew" \) -exec sed -i 's/\r$//' {} \; \
 && chmod +x repl/Admin/init repl/gradlew \
 && ./repl/Admin/init

# Linux/MacOS
#RUN chmod +x repl/Admin/init \
#    && ./repl/Admin/init \
#    && find /root/.isabelle/Isabelle2025-2/contrib -path "*/etc/settings" -type f -exec sed -i 's/\r$//' {} \;

RUN cd repl \
    && chmod +x gradlew \
    && ./gradlew build


CMD ["bash"] 