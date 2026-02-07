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


COPY repl/ /app/repl/
COPY server/ /app/server/
COPY local_gym/ /app/local_gym/
COPY benchmark/ /app/benchmark/
COPY sliding_puzzle/ /app/sliding_puzzle/
COPY test/ /app/test/
COPY *.py /app/
COPY *.txt /app/
COPY *.toml /app/
COPY requirement.txt /app/



RUN mkdir -p /opt \
 && cd /opt \
 && wget https://isabelle.in.tum.de/website-Isabelle2025/dist/Isabelle2025_linux.tar.gz \
 && tar -xf Isabelle2025_linux.tar.gz \
 && mv Isabelle2025 isabelle \
 && rm -f Isabelle2025_linux.tar.gz

ENV ISABELLE_HOME=/opt/isabelle
ENV PATH=$ISABELLE_HOME/bin:$PATH

RUN python -m pip install --upgrade pip \
 && python -m pip install -r /app/requirement.txt

# Global Python deps (no venv)
RUN find /app -type f -name "*.sh" -exec sed -i 's/\r$//' {} \;
RUN find /app/repl/Admin -type f -exec sed -i 's/\r$//' {} \;

RUN chmod +x repl/Admin/init \
 && ./repl/Admin/init \
 && find /root/.isabelle/Isabelle2025/contrib -path "*/etc/settings" -type f -exec sed -i 's/\r$//' {} \;
RUN cd repl \
    && chmod +x gradlew \
    && (./gradlew build || echo "warning: Scala compilation failed, you can compile it manually in the container") \
    && cd ..


CMD ["bash"] 