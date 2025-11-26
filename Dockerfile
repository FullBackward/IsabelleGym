FROM python:3.12-slim


WORKDIR /app


RUN apt-get update && apt-get install -y \
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
 && wget https://isabelle.in.tum.de/dist/Isabelle2025_linux.tar.gz \
 && tar -xf Isabelle2025_linux.tar.gz \
 && mv Isabelle2025 isabelle \
 && rm -f Isabelle2025_linux.tar.gz

ENV ISABELLE_HOME=/opt/isabelle
ENV PATH=$ISABELLE_HOME/bin:$PATH

# Global Python deps (no venv)
RUN python -m pip install --upgrade pip \
 && python -m pip install -r /app/requirement.txt

RUN chmod +x repl/Admin/init \
    && ./repl/Admin/init


RUN cd repl \
    && chmod +x gradlew \
    && (./gradlew build || echo "warning: Scala compilation failed, you can compile it manually in the container") \
    && cd ..


CMD ["bash"] 