# --- add near the top, after color defs ---
ISABELLE_VERSION="2025"

detect_platform() {
    # Normalize an OS token used by Isabelle download names
    case "$(uname -s)" in
        Linux*)   ISABELLE_DIST="linux" ;;
        Darwin*)  ISABELLE_DIST="macos" ;;  # Isabelle uses "macos" in dist names
        *) echo -e "${RED}Unsupported OS$(uname -s)${NC}"; exit 1 ;;
    esac
    ISABELLE_TGZ="Isabelle${ISABELLE_VERSION}_${ISABELLE_DIST}.tar.gz"
    ISABELLE_DIR="Isabelle${ISABELLE_VERSION}"         # extracted folder name
}

# --- replace your check_requirements() with this more flexible one ---
check_requirements() {
    echo -e "${BLUE}check system requirements...${NC}"
    detect_platform
    echo -e "${GREEN}operating system: ${ISABELLE_DIST}${NC}"

    # Java note: Isabelle bundles a JDK; if you rely on that, you can skip java check.
    if command -v java &> /dev/null; then
        JAVA_VERSION=$(java -version 2>&1 | head -n 1 | grep -Eo '"?[0-9]+' | head -n1)
        if [[ ${JAVA_VERSION:-0} -ge 17 ]]; then
            echo -e "${GREEN}Java available: $(java -version 2>&1 | head -n 1)${NC}"
        else
            echo -e "${YELLOW}Java < 17; will rely on Isabelle's bundled JDK if present${NC}"
        fi
    else
        echo -e "${YELLOW}java not found; will rely on Isabelle's bundled JDK${NC}"
    fi

    # python + tools (macOS has BSD tar by default, which is fine here)
    if command -v python3 &> /dev/null; then
        echo -e "${GREEN}Python version: $(python3 --version)${NC}"
    else
        echo -e "${RED}Python3 not found${NC}"
        exit 1
    fi

    for tool in git curl wget tar; do
        if command -v $tool &> /dev/null; then
            echo -e "${GREEN}$tool: installed${NC}"
        else
            echo -e "${RED}$tool not found${NC}"
            exit 1
        fi
    done
}

# --- replace install_isabelle() with this OS-aware version ---
install_isabelle() {
    echo -e "${BLUE}download and install Isabelle ${ISABELLE_VERSION}...${NC}"

    if [ -d "isabelle" ]; then
        echo -e "${YELLOW}Isabelle directory already exists, skip download${NC}"
        return
    fi

    if [ -f "${ISABELLE_TGZ}" ]; then
        echo -e "${GREEN}local Isabelle archive found: ${ISABELLE_TGZ}${NC}"
    else
        echo -e "${BLUE}download Isabelle ${ISABELLE_VERSION} for ${ISABELLE_DIST}...${NC}"
        # NOTE: verify the exact filename on the Isabelle site if this 404s.
        wget "https://isabelle.in.tum.de/dist/${ISABELLE_TGZ}"
    fi

    echo -e "${BLUE}unpack Isabelle...${NC}"
    tar -xf "${ISABELLE_TGZ}"
    mv "${ISABELLE_DIR}" isabelle

    echo -e "${GREEN}Isabelle installation completed${NC}"
}